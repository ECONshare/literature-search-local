"""Reference data fit for a journal: years, volume/issue/pages, APA, exports.

The OpenAlex and Crossref fixtures are the APIs' own records for three papers
a review cited (trimmed to the fields used), fetched 2026-11-04. Semantic
Scholar's follows the shape in its API documentation (its ids are made up).
The field cites these as Becker et al. 2022, Haschka 2022 and Eckert &
Hohberger 2023 - the years of their issues - though all three went online a
year earlier. No test reaches the network: the API clients get a mocked
transport.
"""
import copy
import csv
import io
import re
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from litreview_local import citations, exports, identify, models, services
from litreview_local.config import USER_DEFAULTS, settings
from litreview_local.db import SessionLocal
from litreview_local.pipeline import (SOURCES, crossref, dedupe, http, normalize,
                                      openalex, semantic_scholar)
# conftest stubs this out for every test, so nothing else reaches Crossref.
from litreview_local.services import polish_from_crossref as real_polish

EN = "\N{EN DASH}"

# ------------------------------------------------------------------ fixtures

OPENALEX_BECKER = {
    "id": "https://openalex.org/W3205284412",
    "doi": "https://doi.org/10.1007/s11747-021-00805-y",
    "title": "Revisiting Gaussian copulas to handle endogenous regressors",
    "display_name": "Revisiting Gaussian copulas to handle endogenous regressors",
    "publication_year": 2021,
    "publication_date": "2021-10-11",
    "type": "article",
    "cited_by_count": 312,
    "biblio": {"volume": "50", "issue": "1", "first_page": "46", "last_page": "66"},
    "primary_location": {
        "is_oa": True,
        "landing_page_url": "https://doi.org/10.1007/s11747-021-00805-y",
        "pdf_url": "https://link.springer.com/content/pdf/10.1007/s11747-021-00805-y.pdf",
        "version": "publishedVersion",
        "source": {"id": "https://openalex.org/S92522684",
                   "display_name": "Journal of the Academy of Marketing Science",
                   "issn_l": "0092-0703", "issn": ["0092-0703", "1552-7824"],
                   "type": "journal",
                   "host_organization_name": "Springer Science+Business Media"}},
    "best_oa_location": {
        "landing_page_url": "https://doi.org/10.1007/s11747-021-00805-y",
        "pdf_url": "https://link.springer.com/content/pdf/10.1007/s11747-021-00805-y.pdf"},
    "authorships": [
        {"author_position": "first",
         "author": {"id": "https://openalex.org/A5091257250",
                    "display_name": "Jan-Michael Becker",
                    "orcid": "https://orcid.org/0000-0003-3013-3739"},
         "raw_author_name": "Jan-Michael Becker"},
        {"author_position": "middle",
         "author": {"id": "https://openalex.org/A5029820682",
                    "display_name": "Dorian Proksch",
                    "orcid": "https://orcid.org/0000-0002-8252-3248"},
         "raw_author_name": "Dorian Proksch"},
        {"author_position": "last",
         "author": {"id": "https://openalex.org/A5087109003",
                    "display_name": "Christian M. Ringle",
                    "orcid": "https://orcid.org/0000-0002-7027-8804"},
         "raw_author_name": "Christian M. Ringle"}],
    "abstract_inverted_index": {"Marketing": [0], "researchers": [1], "are": [2],
                                "increasingly": [3], "taking": [4], "advantage": [5]},
}

CROSSREF_BECKER = {
    "DOI": "10.1007/s11747-021-00805-y",
    "type": "journal-article",
    "title": ["Revisiting Gaussian copulas to handle endogenous regressors"],
    "container-title": ["Journal of the Academy of Marketing Science"],
    "short-container-title": ["J. of the Acad. Mark. Sci."],
    "publisher": "Springer Science and Business Media LLC",
    "volume": "50",
    "issue": "1",
    "page": "46-66",
    "published-print": {"date-parts": [[2022, 1]]},
    "published-online": {"date-parts": [[2021, 10, 11]]},
    "issued": {"date-parts": [[2021, 10, 11]]},
    "published": {"date-parts": [[2021, 10, 11]]},
    "journal-issue": {"issue": "1", "published-print": {"date-parts": [[2022, 1]]}},
    "ISSN": ["0092-0703", "1552-7824"],
    "issn-type": [{"value": "0092-0703", "type": "print"},
                  {"value": "1552-7824", "type": "electronic"}],
    "URL": "https://doi.org/10.1007/s11747-021-00805-y",
    "is-referenced-by-count": 306,
    "author": [
        {"given": "Jan-Michael", "family": "Becker", "sequence": "first", "affiliation": []},
        {"given": "Dorian", "family": "Proksch", "sequence": "additional", "affiliation": []},
        {"given": "Christian M.", "family": "Ringle", "sequence": "additional",
         "affiliation": []}],
}

# OpenAlex dates Haschka by the day its DOI was registered (2021-12-17);
# Crossref's own earliest date is 2022, the issue is August 2022.
OPENALEX_HASCHKA = {
    "id": "https://openalex.org/W4200188473",
    "doi": "https://doi.org/10.1177/00222437211070820",
    "display_name": "Handling Endogenous Regressors Using Copulas: A Generalization to "
                    "Linear Panel Models with Fixed Effects and Correlated Regressors",
    "publication_year": 2021,
    "publication_date": "2021-12-17",
    "type": "article",
    "cited_by_count": 56,
    "biblio": {"volume": "59", "issue": "4", "first_page": "860", "last_page": "881"},
    "primary_location": {
        "is_oa": False,
        "landing_page_url": "https://doi.org/10.1177/00222437211070820",
        "pdf_url": None,
        "source": {"display_name": "Journal of Marketing Research",
                   "issn_l": "0022-2437", "issn": ["0022-2437", "1547-7193"],
                   "type": "journal"}},
    "best_oa_location": None,
    "authorships": [{"author_position": "first",
                     "author": {"id": "https://openalex.org/A5069998129",
                                "display_name": "Rouven E. Haschka"}}],
}

CROSSREF_HASCHKA = {
    "DOI": "10.1177/00222437211070820",
    "type": "journal-article",
    "title": ["Handling Endogenous Regressors Using Copulas: A Generalization to Linear "
              "Panel Models with Fixed Effects and Correlated Regressors"],
    "container-title": ["Journal of Marketing Research"],
    "volume": "59",
    "issue": "4",
    "page": "860-881",
    "published-print": {"date-parts": [[2022, 8]]},
    "published-online": {"date-parts": [[2022, 4, 7]]},
    "issued": {"date-parts": [[2022, 4, 7]]},
    "published": {"date-parts": [[2022, 4, 7]]},
    "journal-issue": {"issue": "4", "published-print": {"date-parts": [[2022, 8]]}},
    "ISSN": ["0022-2437", "1547-7193"],
    "URL": "https://doi.org/10.1177/00222437211070820",
    "is-referenced-by-count": 53,
    "author": [{"given": "Rouven E.", "family": "Haschka", "sequence": "first",
                "affiliation": []}],
}

CROSSREF_ECKERT = {
    "DOI": "10.1177/01492063221085913",
    "type": "journal-article",
    "title": ["Addressing Endogeneity Without Instrumental Variables: An Evaluation of "
              "the Gaussian Copula Approach for Management Research"],
    "container-title": ["Journal of Management"],
    "volume": "49",
    "issue": "4",
    "page": "1460-1495",
    "published-print": {"date-parts": [[2023, 4]]},
    "published-online": {"date-parts": [[2022, 3, 28]]},
    "issued": {"date-parts": [[2022, 3, 28]]},
    "journal-issue": {"issue": "4", "published-print": {"date-parts": [[2023, 4]]}},
    "ISSN": ["0149-2063", "1557-1211"],
    "URL": "https://doi.org/10.1177/01492063221085913",
    "author": [{"given": "Christine", "family": "Eckert", "sequence": "first",
                "affiliation": []},
               {"given": "Jan", "family": "Hohberger", "sequence": "additional",
                "affiliation": []}],
}

# Elsevier: the article number in "page" and in "article-number"; no pages.
CROSSREF_DEGRAZIA = {
    "DOI": "10.1016/j.respol.2021.104360",
    "type": "journal-article",
    "title": ["Examination incentives, learning, and patent office outcomes: The use "
              "of examiner\N{RIGHT SINGLE QUOTATION MARK}s amendments at the USPTO"],
    "container-title": ["Research Policy"],
    "volume": "50",
    "issue": "10",
    "page": "104360",
    "article-number": "104360",
    "published-print": {"date-parts": [[2021, 12]]},
    "issued": {"date-parts": [[2021, 12]]},
    "ISSN": ["0048-7333"],
    "URL": "https://doi.org/10.1016/j.respol.2021.104360",
    "author": [
        {"given": "Charles A.W.", "family": "deGrazia", "sequence": "first",
         "affiliation": []},
        {"given": "Nicholas A.", "family": "Pairolero", "sequence": "additional",
         "affiliation": []},
        {"given": "Mike H.M.", "family": "Teodorescu", "sequence": "additional",
         "affiliation": []}],
}

# Semantic Scholar's shape (api.semanticscholar.org/api-docs/graph): the DOI in
# capitals, pages spaced around the dash, no issue, abbreviated given names.
S2_BECKER = {
    "paperId": "5c1f4c2e8d0b7a9e3f6d2c1b0a9e8d7c6b5a4f3e",
    "externalIds": {"MAG": "3205284412", "DOI": "10.1007/S11747-021-00805-Y",
                    "CorpusId": 238654321},
    "url": "https://www.semanticscholar.org/paper/5c1f4c2e8d0b7a9e3f6d2c1b0a9e8d7c6b5a4f3e",
    "title": "Revisiting Gaussian copulas to handle endogenous regressors",
    "abstract": None,
    "venue": "Journal of the Academy of Marketing Science",
    "publicationVenue": {"id": "8a4bd8b0-1f3a-4a3e-9c1d-6a1c8f6e2b11",
                         "name": "Journal of the Academy of Marketing Science",
                         "type": "journal", "alternate_names": ["J Acad Mark Sci"],
                         "issn": "0092-0703", "alternate_issns": ["1552-7824"]},
    "year": 2021,
    "citationCount": 287,
    "openAccessPdf": {"url": "https://link.springer.com/content/pdf/10.1007/s11747-021-00805-y.pdf",
                      "status": "HYBRID"},
    "publicationTypes": ["JournalArticle"],
    "journal": {"name": "Journal of the Academy of Marketing Science",
                "pages": "46 - 66", "volume": "50"},
    "authors": [{"authorId": "2109857123", "name": "Jan-Michael Becker"},
                {"authorId": "1405117268", "name": "Dorian Proksch"},
                {"authorId": "2072915538", "name": "C. Ringle"}],
}

S2_ARXIV = {
    "paperId": "9e0d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d",
    "externalIds": {"ArXiv": "2106.15928", "DOI": "10.48550/arXiv.2106.15928",
                    "CorpusId": 235683045},
    "title": "A preprint on copulas",
    "venue": "arXiv.org",
    "year": 2021,
    "publicationTypes": None,
    "journal": {"name": "ArXiv", "volume": "abs/2106.15928"},
    "authors": [{"authorId": "1", "name": "Ann Author"}],
}


def oa(work=OPENALEX_BECKER) -> dict:
    return normalize.from_openalex(copy.deepcopy(work))


def cr(item=CROSSREF_BECKER) -> dict:
    return normalize.from_crossref(copy.deepcopy(item))


def s2(paper=S2_BECKER) -> dict:
    return normalize.from_semantic_scholar(copy.deepcopy(paper))


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------- normalization

def test_openalex_gives_the_biblio_and_its_year_is_the_online_one():
    rec = oa()
    assert (rec["volume"], rec["issue"], rec["pages"]) == ("50", "1", "46-66")
    # Nothing better known from OpenAlex alone: its year is both.
    assert (rec["year"], rec["online_year"]) == (2021, 2021)
    assert rec["authors"][0] == "Jan-Michael Becker"


def test_crossref_year_is_the_issues():
    rec = cr()
    assert (rec["year"], rec["online_year"]) == (2022, 2021)
    assert (rec["volume"], rec["issue"], rec["pages"]) == ("50", "1", "46-66")
    assert rec["venue"] == "Journal of the Academy of Marketing Science"

    # No published-print on the work: the issue's print date.
    item = copy.deepcopy(CROSSREF_BECKER)
    del item["published-print"]
    assert normalize.from_crossref(item)["year"] == 2022
    # Not in an issue yet: issued, the earliest of print and online.
    del item["journal-issue"]
    assert normalize.from_crossref(item)["year"] == 2021
    # Eckert & Hohberger: online 2022, issue 2023.
    assert (cr(CROSSREF_ECKERT)["year"], cr(CROSSREF_ECKERT)["online_year"]) == (2023, 2022)
    # Haschka: Crossref's own dates are all 2022.
    assert (cr(CROSSREF_HASCHKA)["year"], cr(CROSSREF_HASCHKA)["online_year"]) == (2022, 2022)


def test_crossref_article_number_stands_for_the_pages():
    rec = cr(CROSSREF_DEGRAZIA)
    assert rec["pages"] == "104360"
    item = copy.deepcopy(CROSSREF_DEGRAZIA)
    del item["page"]  # only article-number, as most publishers send it
    assert normalize.from_crossref(item)["pages"] == "104360"
    assert rec["title"].endswith("examiner\N{RIGHT SINGLE QUOTATION MARK}s amendments at the USPTO")


def test_semantic_scholar_journal_gives_volume_and_pages():
    rec = s2()
    assert rec["doi"] == "10.1007/s11747-021-00805-y"
    assert (rec["volume"], rec["issue"], rec["pages"]) == ("50", None, "46-66")
    assert (rec["year"], rec["online_year"]) == (2021, 2021)
    # An arXiv paper's "volume" is its identifier, not a volume.
    assert s2(S2_ARXIV)["volume"] is None


@pytest.mark.parametrize("raw, pages", [
    ("46 - 66", "46-66"), (f"1460{EN}1495", "1460-1495"), ("46--66", "46-66"),
    ("46-46", "46"), ("e0210000", "e0210000"), (" 860-881 ", "860-881"),
    ("S12-S20", "S12-S20"), ("", None), (None, None),
])
def test_pages_are_one_canonical_string(raw, pages):
    assert normalize.clean_pages(raw) == pages


def test_page_range_from_first_and_last():
    assert normalize.page_range("46", "66") == "46-66"
    assert normalize.page_range("104360", "104360") == "104360"  # article number
    assert normalize.page_range("46", None) == "46"
    assert normalize.page_range(None, "66") is None


def test_titles_lose_markup_and_entities():
    assert normalize.clean_text("<i>In vivo</i>  evidence on R&amp;D") == "In vivo evidence on R&D"
    assert normalize.clean_text("CO<sub>2</sub> emissions") == "CO2 emissions"
    assert normalize.clean_text("When x < 5 and y > 2") == "When x < 5 and y > 2"
    assert normalize.clean_text("When A<B on C>D") == "When A<B on C>D"
    assert normalize.clean_text("<jats:italic>Homo</jats:italic> sapiens") == "Homo sapiens"
    item = copy.deepcopy(CROSSREF_BECKER)
    item["container-title"] = ["Journal of Economics &amp; Management Strategy"]
    assert normalize.from_crossref(item)["venue"] == "Journal of Economics & Management Strategy"


# --------------------------------------------------------- the API clients

def test_openalex_search_brings_the_biblio():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={
            "meta": {"count": 1, "db_response_time_ms": 21, "page": None,
                     "per_page": 5, "next_cursor": None},
            "results": [OPENALEX_BECKER]})

    with mock_client(handler) as c:
        recs = openalex.search("gaussian copula endogeneity", max_results=5, client=c)
    assert "select" not in seen  # whole records, biblio included
    assert (recs[0]["volume"], recs[0]["pages"]) == ("50", "46-66")


def test_semantic_scholar_asks_for_the_journal():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"total": 1, "offset": 0, "data": [S2_BECKER]})

    with mock_client(handler) as c:
        recs = semantic_scholar.search("gaussian copulas", max_results=5, client=c)
    assert "journal" in seen["fields"].split(",")
    assert (recs[0]["volume"], recs[0]["pages"]) == ("50", "46-66")


def test_crossref_lookup_by_doi(monkeypatch):
    monkeypatch.setattr(settings, "_user", dict(USER_DEFAULTS, contact_email="me@example.org"))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("10.1007/s11747-021-00805-y"):
            return httpx.Response(200, json={"status": "ok", "message-type": "work",
                                             "message-version": "1.0.0",
                                             "message": CROSSREF_BECKER})
        return httpx.Response(404, text="Resource not found.")

    with mock_client(handler) as c:
        rec = crossref.get_by_doi("10.1007/s11747-021-00805-y", client=c)
        assert crossref.get_by_doi("10.9999/nobody", client=c) is None
    assert (rec["year"], rec["online_year"], rec["issue"]) == (2022, 2021, "1")
    assert seen[0].url.params["mailto"] == "me@example.org"


# ------------------------------------------------------------------ dedupe

def test_merging_takes_crossrefs_print_year_over_openalexs_online_year():
    for order in ([oa(), s2(), cr()], [cr(), oa(), s2()], [s2(), cr(), oa()]):
        merged = dedupe.dedupe(order)
        assert len(merged) == 1
        rec = merged[0]
        assert (rec["year"], rec["online_year"]) == (2022, 2021)
        assert (rec["volume"], rec["issue"], rec["pages"]) == ("50", "1", "46-66")
        assert set(rec["source_ids"]) == {"openalex", "semantic_scholar", "crossref"}


def test_the_earliest_year_of_any_source_is_the_online_year():
    # Crossref says 2022 throughout; OpenAlex knew it in December 2021.
    rec = dedupe.dedupe([oa(OPENALEX_HASCHKA), cr(CROSSREF_HASCHKA)])[0]
    assert (rec["year"], rec["online_year"]) == (2022, 2021)


def test_merging_fills_the_missing_reference_parts():
    bare = oa()
    bare.update(volume=None, issue=None, pages=None)
    rec = dedupe.dedupe([bare, s2()])[0]  # Semantic Scholar has no issue
    assert (rec["volume"], rec["issue"], rec["pages"]) == ("50", None, "46-66")
    assert rec["year"] == 2021  # nobody knows better yet
    rec = dedupe.dedupe([bare, s2(), cr()])[0]
    assert (rec["volume"], rec["issue"], rec["pages"], rec["year"]) == ("50", "1", "46-66", 2022)


# --------------------------------------------------- storing what was found

class _Source:
    def __init__(self, records):
        self.records = records

    def search(self, query, max_results=10, year_from=None, year_to=None):
        return copy.deepcopy(self.records)[:max_results]


def _sources(monkeypatch, **records):
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, _Source(records.get(name, [])))


def test_a_search_stores_the_reference_and_a_later_one_corrects_the_year(
        clean_db, monkeypatch):
    _sources(monkeypatch, openalex=[oa()])
    db = SessionLocal()
    try:
        project = services.create_project(db, "Copulas")
        services.run_search(db, project, "gaussian copula", ["openalex"])
        paper = db.query(models.Paper).one()
        assert (paper.year, paper.online_year) == (2021, 2021)
        assert (paper.volume, paper.issue, paper.pages) == ("50", "1", "46-66")

        # A later search that Crossref answers as well.
        _sources(monkeypatch, openalex=[oa()], crossref=[cr()])
        services.run_search(db, project, "gaussian copula", ["openalex", "crossref"])
        assert db.query(models.Paper).count() == 1
        db.refresh(paper)
        assert (paper.year, paper.online_year) == (2022, 2021)
        assert paper.dedupe_key.endswith("|2022")
        assert set(paper.source_ids) == {"openalex", "crossref", "crossref_year"}
    finally:
        db.close()


def test_a_record_without_a_doi_finds_the_paper_under_its_other_year(
        clean_db, monkeypatch):
    _sources(monkeypatch, openalex=[oa()], crossref=[cr()])
    db = SessionLocal()
    try:
        project = services.create_project(db, "No DOI")
        services.run_search(db, project, "q", ["openalex", "crossref"])
        # Semantic Scholar without the DOI, and with the online year.
        no_doi = copy.deepcopy(S2_BECKER)
        no_doi["externalIds"] = {"CorpusId": 238654321}
        _sources(monkeypatch, semantic_scholar=[s2(no_doi)])
        services.run_search(db, project, "q", ["semantic_scholar"])
        paper = db.query(models.Paper).one()
        assert paper.year == 2022 and "semantic_scholar" in paper.source_ids
    finally:
        db.close()


def test_an_upload_matched_to_a_record_takes_its_reference(clean_db):
    record = identify._jsonable(dedupe.dedupe([oa(), cr()])[0])
    assert {"volume", "issue", "pages", "online_year"} <= set(record)
    db = SessionLocal()
    try:
        project = services.create_project(db, "Uploads")
        paper = models.Paper(project_id=project.id, title="becker2022.pdf",
                             authors=[], issns=[], source_ids={},
                             stage=models.STAGE_RETRIEVAL)
        db.add(paper)
        db.commit()
        ident = models.PaperIdentification(paper_id=paper.id, filename="becker2022.pdf",
                                           described={"title": record["title"]})
        db.add(ident)
        db.commit()
        identify._apply_record(db, ident, paper, record)
        db.refresh(paper)
        assert (paper.year, paper.online_year) == (2022, 2021)
        assert (paper.volume, paper.issue, paper.pages) == ("50", "1", "46-66")
    finally:
        db.close()


# ------------------------------------------------------- Crossref polishing

def _paper(project, rec: dict, **changes) -> models.Paper:
    """A paper as _merge_papers would have stored this canonical record."""
    rec = dict(rec, **changes)
    ids = rec.get("source_ids") or {rec["source"]: rec["source_id"]}
    return models.Paper(
        project_id=project.id, doi=rec.get("doi"), title=rec["title"],
        year=rec.get("year"), online_year=rec.get("online_year"),
        venue=rec.get("venue"), volume=rec.get("volume"), issue=rec.get("issue"),
        pages=rec.get("pages"), work_type=rec.get("work_type"),
        authors=rec.get("authors") or [], issns=[], source_ids=ids,
        dedupe_key=normalize.title_key(rec["title"], rec.get("year")))


def _crossref_answers(records: dict, seen: list) -> httpx.Client:
    """A client for which Crossref knows these works, by DOI."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        doi = unquote(request.url.path.split("/works/", 1)[1]).lower()
        if doi in records:
            return httpx.Response(200, json={"status": "ok", "message-type": "work",
                                             "message-version": "1.0.0",
                                             "message": records[doi]})
        return httpx.Response(404, text="Resource not found.")

    return mock_client(handler)


def _asked(seen: list) -> set[str]:
    return {unquote(r.url.path.split("/works/", 1)[1]) for r in seen}


@pytest.fixture
def polite(monkeypatch):
    monkeypatch.setattr(services, "CROSSREF_DELAY_SECONDS", 0)
    monkeypatch.setattr(settings, "_user",
                        dict(USER_DEFAULTS, contact_email="tester@example.org"))


def test_polish_from_crossref_completes_the_references(clean_db, polite):
    db = SessionLocal()
    try:
        project = services.create_project(db, "Polish")
        becker = _paper(project, oa())                       # OpenAlex only
        haschka = _paper(project, s2(), doi="10.1177/00222437211070820", venue=None,
                         title=cr(CROSSREF_HASCHKA)["title"], volume="59",
                         pages="860-881")                    # S2: no issue, no venue
        settled = _paper(project, cr(CROSSREF_ECKERT))       # Crossref's already
        settled.source_ids = {**settled.source_ids, "crossref_year": True}
        no_doi = _paper(project, oa(), doi=None, title="A working paper")
        preprint = _paper(project, s2(S2_ARXIV), doi="10.48550/arxiv.2106.15928",
                          work_type="preprint")
        unknown = _paper(project, oa(), doi="10.9999/not-registered", title="Unknown")
        papers = [becker, haschka, settled, no_doi, preprint, unknown]
        db.add_all(papers)
        db.commit()

        seen: list = []
        with _crossref_answers({"10.1007/s11747-021-00805-y": CROSSREF_BECKER,
                                "10.1177/00222437211070820": CROSSREF_HASCHKA},
                               seen) as client:
            assert real_polish(db, papers, client=client) == 2
        assert _asked(seen) == {"10.1007/s11747-021-00805-y",
                                "10.1177/00222437211070820", "10.9999/not-registered"}
        assert {r.url.params["mailto"] for r in seen} == {"tester@example.org"}

        db.expire_all()  # read back what was committed
        assert (becker.year, becker.online_year) == (2022, 2021)
        assert becker.dedupe_key.endswith("|2022")
        assert set(becker.source_ids) == {"openalex", "crossref", "crossref_year"}
        assert (haschka.issue, haschka.year, haschka.online_year) == ("4", 2022, 2021)
        assert haschka.venue == "Journal of Marketing Research"
        assert (settled.year, settled.online_year) == (2023, 2022)
        assert (unknown.year, unknown.source_ids) == (2021, {"openalex": "W3205284412"})

        # Asked again, only the DOI Crossref does not know is worth a lookup.
        seen.clear()
        with _crossref_answers({}, seen) as client:
            assert real_polish(db, papers, client=client) == 0
        assert _asked(seen) == {"10.9999/not-registered"}
    finally:
        db.close()


def test_an_article_in_press_gets_its_issue_year_once_the_issue_is_out(clean_db, polite):
    in_press = copy.deepcopy(CROSSREF_HASCHKA)
    for key in ("volume", "issue", "page", "published-print", "journal-issue"):
        del in_press[key]
    in_press["published-online"] = in_press["issued"] = {"date-parts": [[2021, 12, 17]]}
    db = SessionLocal()
    try:
        project = services.create_project(db, "In press")
        paper = _paper(project, cr(in_press))
        db.add(paper)
        db.commit()
        assert (paper.year, paper.volume) == (2021, None)
        assert services.wants_polish(paper)  # Crossref's, but no volume yet
        seen: list = []
        with _crossref_answers({"10.1177/00222437211070820": CROSSREF_HASCHKA},
                               seen) as client:
            assert real_polish(db, [paper], client=client) == 1
        db.expire_all()
        assert (paper.volume, paper.issue, paper.pages) == ("59", "4", "860-881")
        assert (paper.year, paper.online_year) == (2022, 2021)
    finally:
        db.close()


def test_a_year_crossref_has_settled_is_not_reopened():
    # Its year typed in by hand after Crossref gave it 2023.
    paper = models.Paper(doi="10.1177/01492063221085913", title="t", year=2024,
                         online_year=2022, venue="Journal of Management", volume="49",
                         issue="4", pages="1460-1495", work_type="article",
                         source_ids={"crossref": "10.1177/01492063221085913",
                                     "crossref_year": True})
    assert not services.wants_polish(paper)
    # Even handed Crossref's record again, the year stays as it is.
    assert services._take_reference(paper, cr(CROSSREF_ECKERT)) is False
    assert paper.year == 2024
    for kind, wanted in (("book", False), ("book-chapter", True), (None, False)):
        paper.work_type, paper.pages = kind, (None if kind == "book-chapter" else "1-2")
        assert services.wants_polish(paper) is wanted


def test_polish_never_fails_its_caller(clean_db, polite, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda seconds: None)  # retry pauses
    db = SessionLocal()
    try:
        project = services.create_project(db, "Down")
        papers = [_paper(project, oa(), doi=f"10.1007/s{n}", title=f"Paper {n}")
                  for n in range(3)]
        db.add_all(papers)
        db.commit()

        attempts = []

        def unreachable(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            raise httpx.ConnectError("Crossref is unreachable", request=request)

        with mock_client(unreachable) as client:
            assert real_polish(db, papers, client=client) == 0
        # Two lookups failed (each retried thrice), so the third was not tried.
        assert len(_asked(attempts)) == 2 and len(attempts) == 6
        assert all(p.year == 2021 for p in papers)

        # The lookups answer, but the database refuses the write.
        def refuse():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "commit", refuse)
        with _crossref_answers({f"10.1007/s{n}": CROSSREF_BECKER for n in range(3)},
                               []) as client:
            assert real_polish(db, papers, client=client) == 0
        monkeypatch.undo()
        db.expire_all()
        assert all(p.year == 2021 and "crossref" not in p.source_ids for p in papers)
    finally:
        db.close()


# ---------------------------------------------------------------------- APA

def ref(**fields) -> SimpleNamespace:
    """A paper as apa_reference reads it."""
    base = dict(title="Untitled", authors=[], year=None, online_year=None, venue=None,
                volume=None, issue=None, pages=None, doi=None, url=None,
                work_type="article")
    return SimpleNamespace(**dict(base, **fields))


def merged_paper(*records) -> SimpleNamespace:
    rec = dedupe.dedupe(list(records))[0]
    return ref(**{k: rec.get(k) for k in ("title", "authors", "year", "online_year",
                                          "venue", "volume", "issue", "pages", "doi",
                                          "url", "work_type")})


def test_apa_reference_of_a_journal_article():
    paper = merged_paper(oa(), cr())
    assert citations.apa_reference(paper) == (
        "Becker, J.-M., Proksch, D., & Ringle, C. M. (2022). Revisiting Gaussian "
        "copulas to handle endogenous regressors. *Journal of the Academy of "
        f"Marketing Science*, *50*(1), 46{EN}66. https://doi.org/10.1007/s11747-021-00805-y")
    assert citations.years_note(paper) == "online 2021"
    assert citations.apa_inline(paper.authors, paper.year) == "(Becker et al., 2022)"

    two = merged_paper(cr(CROSSREF_ECKERT))
    assert citations.apa_reference(two).startswith(
        "Eckert, C., & Hohberger, J. (2023). Addressing Endogeneity")
    assert f"*Journal of Management*, *49*(4), 1460{EN}1495." in citations.apa_reference(two)
    assert citations.apa_inline(two.authors, two.year) == "(Eckert & Hohberger, 2023)"

    one = merged_paper(oa(OPENALEX_HASCHKA), cr(CROSSREF_HASCHKA))
    assert citations.apa_reference(one).startswith("Haschka, R. E. (2022). Handling")
    assert citations.years_note(one) == "online 2021"


def test_apa_reference_html_italicizes_and_escapes():
    paper = merged_paper(oa(), cr())
    html = citations.apa_reference_html(paper)
    assert "Proksch, D., &amp; Ringle" in html
    assert (f"<i>Journal of the Academy of Marketing Science</i>, <i>50</i>(1), "
            f"46{EN}66.") in html
    odd = ref(title="When x < 5: R&D", authors=["Ann Lee"], year=2020, venue="J")
    assert "When x &lt; 5: R&amp;D." in citations.apa_reference_html(odd)


@pytest.mark.parametrize("name, apa", [
    ("Jan-Michael Becker", "Becker, J.-M."),
    ("Jan\N{HYPHEN}Michael Becker", "Becker, J.-M."),     # OpenAlex's Unicode hyphen
    ("Becker, Jan-Michael", "Becker, J.-M."),
    ("Becker, J.-M.", "Becker, J.-M."),
    ("Jean-Paul Sartre", "Sartre, J.-P."),
    ("Jörn-Steffen Pischke", "Pischke, J.-S."),
    ("Christian M. Ringle", "Ringle, C. M."),
    ("Charles A.W. deGrazia", "deGrazia, C. A. W."),
    ("Harald J. van Heerde", "van Heerde, H. J."),
    ("Eric von Hippel", "von Hippel, E."),
    ("Jan van der Berg", "van der Berg, J."),
    ("Ludwig van Beethoven", "van Beethoven, L."),
    ("Jacobus Henricus van 't Hoff", "van 't Hoff, J. H."),
    ("Charles de Gaulle", "de Gaulle, C."),
    ("Christophe Van den Bulte", "Van den Bulte, C."),
    ("John Van Reenen", "Van Reenen, J."),
    ("Rafael Di Tella", "Di Tella, R."),
    ("Rafael La Porta", "La Porta, R."),
    ("Van Morrison", "Morrison, V."),                   # a given name, not a particle
    ("Thanh Van Nguyen", "Nguyen, T. V."),              # a Vietnamese middle name
    ("Martin Luther King Jr.", "King, M. L., Jr."),
    ("King, Martin Luther, Jr.", "King, M. L., Jr."),
    ("Takashi Ii", "Ii, T."),                           # a surname, not a numeral
    ("Øyvind Bøhren", "Bøhren, Ø."),
    ("World Health Organization", "World Health Organization"),
    ("Madonna", "Madonna"),
])
def test_apa_author_names(name, apa):
    assert citations.apa_author(name) == apa


def test_apa_inline_keeps_particles():
    assert citations.apa_inline(["Harald J. van Heerde", "Peter S. H. Leeflang"],
                                2020) == "(van Heerde & Leeflang, 2020)"
    assert citations.apa_inline([], None) == "(Anon., n.d.)"


def test_apa_lists_twenty_authors_and_shortens_twenty_one():
    names = [f"Ann Surname{chr(65 + i)}" for i in range(25)]  # SurnameA ... SurnameY
    twenty = citations.apa_reference(ref(authors=names[:20], year=2020, title="T"))
    assert twenty.startswith("SurnameA, A., SurnameB, A.,")
    assert "SurnameS, A., & SurnameT, A. (2020)." in twenty
    many = citations.apa_reference(ref(authors=names, year=2020, title="T"))
    shown = re.findall(r"Surname([A-Y])", many)
    assert shown == [chr(65 + i) for i in range(19)] + ["Y"]  # first 19, then the last
    assert "SurnameS, A., \N{HORIZONTAL ELLIPSIS} SurnameY, A. (2020)." in many
    assert "&" not in many


def test_apa_reference_of_other_kinds_of_work():
    article_number = merged_paper(cr(CROSSREF_DEGRAZIA))
    assert citations.apa_reference(article_number) == (
        "deGrazia, C. A. W., Pairolero, N. A., & Teodorescu, M. H. M. (2021). "
        "Examination incentives, learning, and patent office outcomes: The use of "
        "examiner\N{RIGHT SINGLE QUOTATION MARK}s amendments at the USPTO. *Research "
        "Policy*, *50*(10), Article 104360. https://doi.org/10.1016/j.respol.2021.104360")
    book = ref(work_type="book", authors=["Joshua D. Angrist", "Jörn-Steffen Pischke"],
               year=2009, title="Mostly Harmless Econometrics: An Empiricist's Companion",
               venue="Princeton University Press", doi="10.1515/9781400829828")
    assert citations.apa_reference(book) == (
        "Angrist, J. D., & Pischke, J.-S. (2009). *Mostly Harmless Econometrics: An "
        "Empiricist's Companion*. Princeton University Press. "
        "https://doi.org/10.1515/9781400829828")
    chapter = ref(work_type="book-chapter", authors=["James J. Heckman"], year=2007,
                  title="Econometric evaluation of social programs",
                  venue="Handbook of Econometrics", pages="4779-4874")
    assert citations.apa_reference(chapter) == (
        "Heckman, J. J. (2007). Econometric evaluation of social programs. In "
        f"*Handbook of Econometrics* (pp. 4779{EN}4874).")
    report = ref(work_type="report", authors=["World Health Organization"], year=2020,
                 title="World health statistics 2020", url="https://www.who.int/data")
    assert citations.apa_reference(report) == (
        "World Health Organization. (2020). *World health statistics 2020*. "
        "https://www.who.int/data")
    nobody = ref(title="Does it matter?", venue="The Economist")
    assert citations.apa_reference(nobody) == "Does it matter? (n.d.). *The Economist*."


def test_the_reference_gives_the_issue_year_and_the_note_the_online_one():
    paper = ref(authors=["Ann Lee"], title="T", venue="J", year=2022, online_year=2021)
    assert "(2022)." in citations.apa_reference(paper)
    assert citations.years_note(paper) == "online 2021"
    assert citations.years_note(ref(year=2022, online_year=2022)) is None
    assert citations.years_note(ref(year=2022)) is None
    # No issue year known at all: the online year rather than n.d.
    assert "(2021)." in citations.apa_reference(ref(authors=["Ann Lee"], online_year=2021))


# ------------------------------------------------------------------ exports

def parse_bibtex(text: str) -> list[dict]:
    """Entries as {"type", "key", "fields"}, counting braces the way BibTeX
    does (an escaped brace counts too)."""
    entries = []
    for head in re.finditer(r"@(\w+)\{([^,\s]+),", text):
        fields, i = {}, head.end()
        while True:
            field = re.compile(r"\s*(\w+)\s*=\s*\{").match(text, i)
            if not field:
                break
            depth, j = 1, field.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            fields[field.group(1)] = text[field.end():j - 1]
            i = re.compile(r"\s*,?").match(text, j).end()
        assert re.compile(r"\s*\}").match(text, i), "entry not closed"
        entries.append({"type": head.group(1), "key": head.group(2), "fields": fields})
    return entries


def unlatex(value: str) -> str:
    return re.sub(r"\\([{}&%$#_])", r"\1", value)


def parse_ris(text: str) -> list[dict[str, list[str]]]:
    records, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            continue
        tag, sep, value = line.partition("  - ")
        assert sep and re.fullmatch(r"[A-Z][A-Z0-9]", tag), f"not RIS: {line!r}"
        if tag == "ER":
            records.append(current)
            current = {}
        else:
            current.setdefault(tag, []).append(value)
    assert not current, "record without ER"
    return records


def row(paper: SimpleNamespace, **extra) -> dict:
    """What exports.paper_row gives for this paper."""
    defaults = dict(oa_url=None, citation_count=None, status="included", note=None,
                    abstract=None, tags=[])
    return {**vars(paper), **defaults, **extra}


BECKER_ROW = row(merged_paper(oa(), cr()), abstract="Marketing researchers are\n"
                 "increasingly taking advantage.", tags=["copula", "endogeneity"])
ODD_ROW = row(ref(title="R&D, 50% {growth} and the Müller effect", year=2019,
                  authors=["Jörn-Steffen Pischke", "Øyvind Bøhren", "Harald J. van Heerde",
                           "Martin Luther King Jr.", "World Health Organization"],
                  venue="Économie & Statistique", volume="7", issue="2",
                  pages="e0210000", doi="10.1234/odd_doi"))


def test_bibtex_reads_back_with_names_volume_pages_and_unicode():
    text = exports.to_bibtex([BECKER_ROW, ODD_ROW])
    becker, odd = parse_bibtex(text)
    assert (becker["type"], becker["key"]) == ("article", "becker2022revisiting")
    f = becker["fields"]
    assert f["author"].split(" and ") == ["Becker, Jan-Michael", "Proksch, Dorian",
                                          "Ringle, Christian M."]
    assert (f["journal"], f["year"], f["volume"], f["number"], f["pages"]) == (
        "Journal of the Academy of Marketing Science", "2022", "50", "1", "46--66")
    assert f["doi"] == "10.1007/s11747-021-00805-y"
    assert f["url"] == "https://doi.org/10.1007/s11747-021-00805-y"

    f = odd["fields"]
    assert odd["key"] == "pischke2019rd"
    assert f["author"].split(" and ") == [
        "Pischke, Jörn-Steffen", "Bøhren, Øyvind", "van Heerde, Harald J.",
        "King, Jr., Martin Luther", "{World Health Organization}"]
    assert f["title"] == r"R\&D, 50\% \{growth\} and the Müller effect"
    assert unlatex(f["title"]) == ODD_ROW["title"]
    assert unlatex(f["journal"]) == "Économie & Statistique"
    assert (f["pages"], f["doi"]) == ("e0210000", "10.1234/odd_doi")
    assert "Müller" in text and "Øyvind" in text  # Unicode kept, not {\"u}


def test_bibtex_keys_are_stable_and_entry_types_follow_the_work():
    keys = [e["key"] for e in parse_bibtex(exports.to_bibtex([BECKER_ROW, ODD_ROW]))]
    again = [e["key"] for e in parse_bibtex(exports.to_bibtex([ODD_ROW, BECKER_ROW]))]
    assert keys == list(reversed(again))
    twin = dict(BECKER_ROW, doi="10.1007/other")
    assert [e["key"] for e in parse_bibtex(exports.to_bibtex([BECKER_ROW, twin]))] == [
        "becker2022revisiting", "becker2022revisitingb"]

    kinds = [("book", "book", "publisher"), ("book-chapter", "incollection", "booktitle"),
             ("conference-paper", "inproceedings", "booktitle"),
             ("report", "techreport", "institution"),
             ("working-paper", "techreport", "institution"),
             ("preprint", "misc", "howpublished"), ("dissertation", "phdthesis", "school"),
             (None, "article", "journal"), ("dataset", "misc", "howpublished")]
    rows = [dict(BECKER_ROW, work_type=kind, venue="The Venue") for kind, _, _ in kinds]
    for entry, (kind, entry_type, container) in zip(
            parse_bibtex(exports.to_bibtex(rows)), kinds):
        assert entry["type"] == entry_type, kind
        assert entry["fields"][container] == "The Venue", kind
    report = parse_bibtex(exports.to_bibtex([rows[4]]))[0]["fields"]
    assert report["type"] == "Working Paper"


def test_ris_reads_back_with_names_volume_pages_and_unicode():
    becker, odd = parse_ris(exports.to_ris([BECKER_ROW, ODD_ROW]))
    assert becker["TY"] == ["JOUR"]
    assert becker["AU"] == ["Becker, Jan-Michael", "Proksch, Dorian", "Ringle, Christian M."]
    assert becker["PY"] == ["2022"]
    assert becker["JO"] == becker["T2"] == ["Journal of the Academy of Marketing Science"]
    assert (becker["VL"], becker["IS"], becker["SP"], becker["EP"]) == (
        ["50"], ["1"], ["46"], ["66"])
    assert becker["DO"] == ["10.1007/s11747-021-00805-y"]
    assert becker["UR"] == ["https://doi.org/10.1007/s11747-021-00805-y"]
    assert becker["AB"] == ["Marketing researchers are increasingly taking advantage."]
    assert becker["KW"] == ["copula", "endogeneity"]

    assert odd["AU"] == ["Pischke, Jörn-Steffen", "Bøhren, Øyvind", "van Heerde, Harald J.",
                         "King, Martin Luther, Jr.", "World Health Organization,"]
    assert odd["TI"] == ["R&D, 50% {growth} and the Müller effect"]
    assert odd["SP"] == ["e0210000"] and "EP" not in odd

    kinds = {"book": ("BOOK", "PB"), "book-chapter": ("CHAP", "T2"),
             "report": ("RPRT", "PB"), "preprint": ("UNPB", "PB"), None: ("JOUR", "T2")}
    records = parse_ris(exports.to_ris(
        [dict(BECKER_ROW, work_type=kind, venue="The Venue") for kind in kinds]))
    for record, (ty, container) in zip(records, kinds.values()):
        assert record["TY"] == [ty] and record[container] == ["The Venue"]


def test_csv_has_the_reference_columns():
    rows = list(csv.DictReader(io.StringIO(exports.to_csv([BECKER_ROW]))))
    assert {"volume", "issue", "pages", "online_year"} <= set(rows[0])
    assert (rows[0]["year"], rows[0]["online_year"], rows[0]["volume"], rows[0]["issue"],
            rows[0]["pages"]) == ("2022", "2021", "50", "1", "46-66")
    assert rows[0]["authors"] == "Jan-Michael Becker; Dorian Proksch; Christian M. Ringle"


def test_paper_row_carries_the_new_fields():
    paper = models.Paper(title="T", authors=["Ann Lee"], year=2022, online_year=2021,
                         venue="J", volume="50", issue="1", pages="46-66",
                         work_type="article", doi="10.1/x")
    got = exports.paper_row(paper)
    assert {k: got[k] for k in ("year", "online_year", "volume", "issue", "pages",
                                "work_type", "tags")} == {
        "year": 2022, "online_year": 2021, "volume": "50", "issue": "1",
        "pages": "46-66", "work_type": "article", "tags": []}


def test_a_crossref_id_from_before_the_year_rule_still_gets_its_year_checked():
    """Liengaard et al.: a Crossref id merged in by a build that
    did not compare years, the online year (2024) kept, the issue 2025."""
    paper = models.Paper(doi="10.1007/x", title="t", year=2024, volume="53",
                         issue="1", pages="279-299", work_type="article",
                         source_ids={"openalex": "W1", "crossref": "10.1007/x"})
    assert services.wants_polish(paper)
