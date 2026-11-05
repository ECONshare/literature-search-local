"""Old quotes re-checked, offline runs, back matter."""
import httpx

from litreview_local import citations, models, research
from litreview_local.db import SessionLocal
from litreview_local.pipeline import SOURCES

from test_pipeline_e2e import BODY, FakeSource, PAPERS, work_queue


def test_an_upgraded_library_rechecks_its_old_quotes(claude, monkeypatch):
    from litreview_local import pdfs
    from test_pipeline_e2e import make_pdf

    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS[:1]))
    monkeypatch.setattr(pdfs, "download_pdf", lambda paper, client=None: (
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY), (True, None))[1])
    project = claude.call("create_project", name="Old build")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    db = SessionLocal()
    try:
        annotation = db.query(models.Annotation).first()
        # As an earlier build stored them: unchecked, one invented, one the
        # same passage again under different spacing.
        annotation.findings = [
            {"title": "ok", "quote": BODY[1], "page": 1, "why": "w"},
            {"title": "invented", "quote": BODY[1][:-1] + " and never at weekends.",
             "page": 1, "why": "w"},
            # The same sentence with a soft hyphen in it, as a PDF hands it
            # over: a different key, the same passage.
            {"title": "again", "quote": BODY[1].replace("average", "aver­age"),
             "page": 1, "why": "w"},
        ]
        db.commit()
        keys = [citations.finding_key(annotation.id, f) for f in annotation.findings]
        assert len(set(keys)) == 3
    finally:
        db.close()

    stats = research.reverify_findings()
    assert stats["rejected"] == 1 and stats["duplicates"] == 1
    assert research.reverify_findings()["annotations"] == 0  # once only

    db = SessionLocal()
    try:
        annotation = db.query(models.Annotation).first()
        cmap = citations.citation_map([annotation])
        assert keys[0] in cmap and keys[1] not in cmap  # the invented one is out
        assert cmap[keys[2]]["alias_of"] == keys[0]      # the repeat still resolves
        digest = research.evidence_digest(cmap)
        assert digest.count(BODY[1]) == 1                 # and is listed once
        assert f"[CIT:{keys[0]}]" in digest and keys[1] not in digest
    finally:
        db.close()
    del started


class _Offline:
    def search(self, query, max_results=50, year_from=None, year_to=None):
        raise httpx.ConnectError("[WinError 10061] No connection could be made")


def test_a_search_with_no_internet_fails_and_says_why(claude, monkeypatch):
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, _Offline())
    project = claude.call("create_project", name="Offline")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?",
                          concept_groups=[["remote work"], ["wellbeing"]],
                          query="remote work", headline="Remote work")
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, started["question_id"])
        assert rq.status == models.RQ_FAILED
        assert "couldn't reach the internet" in rq.error
        assert "Search again" in rq.error and "WinError" not in rq.error
        assert not rq.summary_html
    finally:
        db.close()


def test_one_busy_source_is_said_plainly(claude, monkeypatch):
    class Busy:
        def search(self, *a, **k):
            raise RuntimeError("Request to https://api.semanticscholar.org/graph/v1/"
                               "paper/search failed after 3 attempts (rate-limited; "
                               "HTTP 429)")
    monkeypatch.setitem(SOURCES, "openalex", FakeSource(PAPERS))
    monkeypatch.setitem(SOURCES, "semantic_scholar", Busy())
    monkeypatch.setitem(SOURCES, "crossref", FakeSource([]))
    project = claude.call("create_project", name="Busy")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?",
                          concept_groups=[["remote work"], ["wellbeing"]],
                          query="remote work", headline="Remote work")
    listed = claude.call("list_questions", project_id=project["project_id"])
    notes = listed["questions"][0]["run_notes"]
    assert notes == ["Semantic Scholar was busy (rate-limited) and returned nothing "
                     "this time; the other sources were searched"]
    assert "rate-limited" in listed["questions"][0]["status_detail"]
    del started


def test_reference_lists_are_not_read():
    from litreview_local import paper_texts

    body = "Main finding text about copulas and endogeneity. " * 40
    refs = "\n".join(f"Author{'ABCDEFGHIJKL'[i]}, A. (20{10 + i}). A paper on things. "
                     f"Journal, {i}(2), 1-20. https://doi.org/10.1000/x{i}" for i in range(12))
    content = (f"<!-- page 1 -->\n# Introduction\n{body}\n\n"
               f"<!-- page 2 -->\n{body}\n# References\n{refs[:400]}\n\n"
               f"<!-- page 3 -->\n{refs[400:]}\n\n"
               "<!-- page 4 -->\n# Appendix A\nThe extra table that matters.\n"
               "It is read: it holds evidence, not references.\n"
               "Row one shows the effect.\nRow two shows the cost.\n")
    pages = research.reading_pages(content)
    assert "AuthorA" not in " ".join(pages.values())
    assert "Main finding" in pages[2]
    assert "The extra table that matters." in pages[4]

    # A manual whose every function ends in a short "References": the
    # documentation in between is all read.
    doc = "Usage: iv(x, y). Arguments: x, the data. Details: it estimates. " * 30
    manual = "<!-- page 1 -->\n# Intro\n" + doc + "\n"
    for n in range(2, 6):
        manual += (f"<!-- page {n} -->\n# Function f{n}\n{doc}\n### References\n"
                   "Park, S. (2012). Handling endogeneity. Marketing Science.\n\n")
    kept = research.reading_pages(manual)
    assert sorted(kept) == [1, 2, 3, 4, 5]
    assert all("Usage: iv" in kept[n] for n in range(2, 6))

    # A long list under another heading - "Publications listed in Table A1" -
    # is cut all the same.
    listed = (f"<!-- page 1 -->\n# Results\n{body}\n{body}\n\n"
              f"<!-- page 2 -->\n# Publications listed in Table A1\n{refs}\n")
    assert "AuthorF" not in " ".join(research.reading_pages(listed).values())

    # A "References" heading early in a paper is not back matter.
    early = "<!-- page 1 -->\n# References to earlier work\n" + body
    assert research.reading_pages(early) == paper_texts.split(early)


def test_excluding_a_cited_paper_says_what_it_costs(claude, monkeypatch, client):
    from test_verification import _answered

    question_id, keys = _answered(claude, monkeypatch)
    paper_id = claude.call("get_answer", question_id=question_id)["answers"][0][
        "citations"][0]["paper_id"]
    response = client.post(f"/papers/{paper_id}/status", data={"status": "excluded"},
                           headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "it was cited in RQ1&#39;s answer" in response.text or \
        "it was cited in RQ1's answer" in response.text
    # Nothing to warn about when it is not cited anywhere.
    response = client.post(f"/papers/{paper_id}/status", data={"status": "included"},
                           headers={"HX-Request": "true"})
    assert "it was cited" not in response.text


def test_an_old_build_s_highlights_are_redrawn_once(claude, monkeypatch):
    from litreview_local import pdfs
    from test_pipeline_e2e import make_pdf

    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS[:1]))
    monkeypatch.setattr(pdfs, "download_pdf", lambda paper, client=None: (
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY), (True, None))[1])
    project = claude.call("create_project", name="Redraw")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    db = SessionLocal()
    try:
        annotation = db.query(models.Annotation).first()
        key = citations.finding_key(annotation.id, annotation.findings[0])
        # As an older build left it: unplaced, no marker.
        annotation.findings = [dict(f, matched=False, rects=[], located="text",
                                    hv=None) for f in annotation.findings]
        db.commit()
    finally:
        db.close()
    stats = research.rehighlight_old()
    assert stats["annotations"] == 1 and stats["highlighted"] >= 1
    assert research.rehighlight_old()["annotations"] == 0  # once only
    db = SessionLocal()
    try:
        annotation = db.query(models.Annotation).first()
        finding = annotation.findings[0]
        assert finding["matched"] and finding["rects"] and finding["located"] == "pdf"
        assert citations.finding_key(annotation.id, finding) == key  # keys stay
    finally:
        db.close()


def test_search_papers_finds_words_and_authors(claude, monkeypatch):
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS))
    project = claude.call("create_project", name="Search words")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?",
                concept_groups=[["remote work"], ["wellbeing"]],
                query="remote work", headline="Remote work")
    found = claude.call("search_papers", project_id=project["project_id"],
                        query="Berg")             # an author, not in any title
    assert [p["authors"][0] for p in found["papers"]] == ["Ann Berg"]
    found = claude.call("search_papers", project_id=project["project_id"],
                        query="wellbeing remote study 3")
    assert len(found["papers"]) == 1


def test_claude_desktop_connected_is_its_own_call(claude):
    from litreview_local import tasks
    from litreview_local.web import _app_seen

    tasks._seen_cache.clear()
    db = SessionLocal()
    try:
        tasks.note_client(db, "claude-code")
        assert _app_seen(db, "claude-ai") is None      # Code is not Desktop
        assert _app_seen(db, "claude-code") is not None
        tasks.note_client(db, "claude-ai")
        assert _app_seen(db, "claude-ai") is not None
        later = models.utcnow().replace(year=models.utcnow().year + 1)
        assert _app_seen(db, "claude-ai", later) is None  # before the entry existed
    finally:
        db.close()


def test_an_old_library_s_texts_are_cleaned_once(claude, monkeypatch):
    """Texts stored by an older build keep ligatures and soft hyphens; they
    are cleaned once at startup, so searching finds what the paper says."""
    from litreview_local import pdfs
    from test_pipeline_e2e import make_pdf

    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS[:1]))
    monkeypatch.setattr(pdfs, "download_pdf", lambda paper, client=None: (
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY), (True, None))[1])
    project = claude.call("create_project", name="Old texts")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    db = SessionLocal()
    try:
        text = db.query(models.PaperText).first()
        text.content = text.content + "\nFirm eﬃciency levels rose by twelve per­cent."
        db.query(models.AppState).filter_by(key="texts_cleaned_v1").delete()
        db.commit()
    finally:
        db.close()
    assert claude.call("search_paper_text", query="efficiency levels")["papers_matched"] == 0
    stats = research.clean_old_texts()
    assert stats["papers"] == 1
    assert research.clean_old_texts() == {}  # once per library
    found = claude.call("search_paper_text", query="efficiency levels")
    assert found["papers_matched"] == 1
    # A query written with the ligature finds it too.
    assert claude.call("search_paper_text", query="eﬃciency")["papers_matched"] == 1


def test_add_evidence_says_how_the_source_words_it(claude, monkeypatch):
    from test_verification import _answered

    question_id, _ = _answered(claude, monkeypatch)
    paper = claude.call("search_papers", question_id=question_id)["papers"][0]
    first = claude.call("add_evidence", question_id=question_id,
                        paper_id=paper["paper_id"],
                        quote="Managers reported no loss of productivity over-all.",
                        why="w")
    assert first["quote_in_source"] == "Managers reported no loss of productivity overall."
    again = claude.call("add_evidence", question_id=question_id,
                        paper_id=paper["paper_id"],
                        quote="Managers reported no loss of productivity overall.",
                        why="w")
    assert again["already_there"] and "quote_in_source" not in again
    # And the highlights of a key are found by the key alone.
    got = claude.call("get_highlights", keys=[first["citation_key"]])
    assert got["papers"][0]["highlights"][0]["key"] == first["citation_key"]


def test_numbered_advice_is_read_and_unmarked_lists_are_cut():
    """Real cases: a paper's numbered recommendations are its
    evidence, not a reference list; a list under a plain 'References' line,
    blank-line separated, and one an older build marked with '## ' per
    wrapped line, are reference lists."""
    body = "The method rests on non-normality and an exogeneity condition. " * 40
    advice = "\n".join(
        f"{n}. Treat marginal non-normality as necessary, never sufficient. "
        "Confirming that P is non-normal clears only one of several hurdles."
        for n in range(1, 9))
    content = (f"<!-- page 1 -->\n# Introduction\n{body}\n\n"
               f"<!-- page 2 -->\n{body}\n# Recommendations\n{advice}\n")
    pages = research.reading_pages(content)
    assert "8. Treat marginal" in " ".join(pages.values())

    refs = "\n\n".join([
        'Amsler, C., A. Prokhorov and P. Schmidt (2014), "Endogeneity in '
        'stochastic frontier models." *Working Paper*, Michigan State University.',
        'Battese. G.E. and T.J. Coelli (1995), "A model for technical '
        'inefficiency effects." *Empirical Economics* 20, 325-332.',
        'Park, S. and S. Gupta (2012), "Handling endogenous regressors by joint '
        'estimation using copulas." *Marketing Science* 31, 567-586.',
        'Tran, K.C. and E.G. Tsionas (2015), "Endogeneity in stochastic frontier '
        'models: Copula approach." *Economics Letters* 133, 85-88.',
        'Greene, W. (2005), "Reconsidering heterogeneity in panel data '
        'estimators." *Journal of Econometrics* 126, 269-303.',
        'Kumbhakar, S.C. and C.A.K. Lovell (2000), *Stochastic Frontier '
        'Analysis*. Cambridge University Press.'])
    plain = (f"<!-- page 1 -->\n# Model\n{body}\n\n"
             f"<!-- page 2 -->\n{body}\n**References**\n{refs}\n")
    kept = " ".join(research.reading_pages(plain).values())
    assert "Amsler" not in kept and "exogeneity condition" in kept

    marked = "\n".join(
        f"## {name}, C., A. Vomberg, and S. Muehlhaeuser ({year}). Design matters.\n"
        f"Journal of Marketing, {n}({n}), 1-{20 + n}."
        for n, (name, year) in enumerate([("Homburg", 2020), ("Becker", 2022),
                                          ("Park", 2012), ("Haschka", 2022),
                                          ("Liengaard", 2025), ("Tran", 2015),
                                          ("Greene", 2005)], start=1))
    old = (f"<!-- page 1 -->\n# Results\n{body}\n{body}\n\n"
           f"<!-- page 2 -->\n# Publications listed in Table A1\n{marked}\n")
    assert "Homburg" not in " ".join(research.reading_pages(old).values())


def test_tables_after_the_references_are_read_and_fragments_do_not_split():
    body = "The estimator is consistent under the stated conditions. " * 40
    refs = "\n\n".join(
        f"{name}, {initial}. ({year}), \"A study of frontiers.\" *Journal* {vol}, 1-{10 + vol}."
        for name, initial, year, vol in [("Amsler", "C", 2014, 3), ("Battese", "G", 1995, 20),
                                         ("Greene", "W", 2005, 126), ("Park", "S", 2012, 31),
                                         ("Tran", "K", 2015, 133), ("Kumbhakar", "S", 2000, 7)])
    tables = ("**Table 1: Estimated MSE: $\\rho = 0.0$**\n"
              "| n | OLS | Copula |\n|---|---|---|\n| 200 | 0.41 | 0.12 |\n"
              "| 400 | 0.39 | 0.07 |\n\\* See Table 1.\n")
    content = (f"<!-- page 1 -->\n# Model\n{body}\n\n"
               f"<!-- page 2 -->\n{body}\n## References\n{refs}\n\n"
               f"<!-- page 3 -->\n{tables}")
    kept = " ".join(research.reading_pages(content).values())
    assert "Amsler" not in kept and "Kumbhakar" not in kept
    assert "Estimated MSE" in kept and "| 400 | 0.39 | 0.07 |" in kept

    # Wrapped fragments an older build marked as headings do not cut the
    # list into one-line sections.
    entries = "\n".join(
        f"{name}, C., A. Vomberg, and S. Muehlhaeuser ({year}). Design and the\n"
        f"## ing role of design, retailer-strategy, and\nJournal of Marketing, {n}({n}), 1-9."
        for n, (name, year) in enumerate([("Homburg", 2020), ("Becker", 2022), ("Park", 2012),
                                          ("Haschka", 2022), ("Liengaard", 2025),
                                          ("Tran", 2015), ("Greene", 2005)], start=1))
    old = (f"<!-- page 1 -->\n# Results\n{body}\n{body}\n\n"
           f"<!-- page 2 -->\n# Publications listed in Table A1\n{entries}\n")
    assert "Homburg" not in " ".join(research.reading_pages(old).values())
