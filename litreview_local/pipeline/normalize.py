"""Normalize records from each source into one canonical dict shape.

Canonical record:
    {
        "doi": "10.1234/abc" | None,        # lowercase, no https://doi.org/ prefix
        "title": str,                       # plain text: no markup, no entities
        "abstract": str | None,
        "year": int | None,                 # the version of record's year (below)
        "online_year": int | None,          # the earliest year it appeared
        "venue": str | None,
        "volume": str | None,               # "50"
        "issue": str | None,                # "1"
        "pages": str | None,                # "46-66", a first page alone ("46"),
                                            # or an article number ("104360")
        "issn_l": str | None,               # linking ISSN of the venue
        "issns": [str, ...],                # every ISSN variant reported
        "work_type": str | None,            # article / book / preprint / ...
        "authors": [str, ...],              # "Given Family", as the source has it
        "citation_count": int | None,
        "url": str | None,                  # landing page
        "oa_url": str | None,               # open-access location if known
        "source": "openalex" | "semantic_scholar" | "crossref",
        "source_id": str,
    }

Years. A journal article usually goes online months before the issue that
carries it, and the field cites the issue's year: Becker, Proksch and Ringle
went online in October 2021 and are cited as 2022, the year of JAMS 50(1).
So `year` is the version of record's year where a source states it -
Crossref's published-print, then the print date of the journal issue, then
its issued date - and `online_year` is the earliest year a source gives.
OpenAlex's publication_year is the earliest date it knows (often the day the
DOI was registered), and Semantic Scholar's year is taken the same way: both
are `online_year`, and `year` only until a better one is known. dedupe.py
lets Crossref's year win a merge, and services.polish_from_crossref asks
Crossref for the papers that came without it.
"""
import html
import re

# Canonical work types. Journal quality ratings only apply to `article`, so
# the rest have to stay distinguishable: in economics a book or a working
# paper is frequently the most important source, and treating it as an
# unrated journal would bury it.
TYPE_ARTICLE = "article"
TYPE_REVIEW = "review"
TYPE_BOOK = "book"
TYPE_CHAPTER = "book-chapter"
TYPE_PREPRINT = "preprint"
TYPE_REPORT = "report"
TYPE_OTHER = "other"

_S2_TYPES = {
    "JournalArticle": TYPE_ARTICLE,
    "Review": TYPE_REVIEW,
    "Book": TYPE_BOOK,
    "BookSection": TYPE_CHAPTER,
    "Conference": TYPE_OTHER,
    "Dataset": TYPE_OTHER,
    "Editorial": TYPE_OTHER,
}

_CROSSREF_TYPES = {
    "journal-article": TYPE_ARTICLE,
    "book": TYPE_BOOK,
    "monograph": TYPE_BOOK,
    "edited-book": TYPE_BOOK,
    "reference-book": TYPE_BOOK,
    "book-chapter": TYPE_CHAPTER,
    "book-part": TYPE_CHAPTER,
    "book-section": TYPE_CHAPTER,
    "posted-content": TYPE_PREPRINT,  # Crossref's label for preprints
    "report": TYPE_REPORT,
    "report-component": TYPE_REPORT,
    "proceedings-article": TYPE_OTHER,
    "dissertation": TYPE_OTHER,
}


def _s2_work_type(types: list) -> str | None:
    """Semantic Scholar reports a list; take the first one we recognize."""
    for t in types or []:
        if t in _S2_TYPES:
            return _S2_TYPES[t]
    return TYPE_OTHER if types else None


def _crossref_work_type(raw: str | None) -> str | None:
    if not raw:
        return None
    return _CROSSREF_TYPES.get(raw, TYPE_OTHER)


def boolean_string(groups: list[list[str]]) -> str:
    """Canonical AND-of-ORs query, e.g. ("a b" OR c) AND (d OR e).

    groups is the LLM's concept structure: outer list ANDed, inner variants
    ORed, multi-word terms double-quoted. Used for display and by sources
    whose API takes this standard syntax (OpenAlex). Returns "" when there
    are no groups.
    """
    def term(t: str) -> str:
        # Quotes/parens inside a term would corrupt the boolean syntax;
        # quoting neutralizes operator words (AND/OR/NOT are only special
        # outside quotes), so also quote a bare term that IS one.
        t = " ".join(t.replace('"', " ").replace("(", " ").replace(")", " ").split())
        return f'"{t}"' if (" " in t or t.upper() in ("AND", "OR", "NOT")) else t

    parts = []
    for g in groups:
        terms = [s for s in (term(t) for t in g) if s]
        if not terms:
            continue
        inner = " OR ".join(terms)
        parts.append(f"({inner})" if len(terms) > 1 else terms[0])
    return " AND ".join(parts)


def clean_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    doi = raw.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = doi.strip("/ ")
    return doi or None


def title_key(title: str, year: int | None) -> str:
    """Fallback dedupe key: normalized title + year."""
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    return f"{t}|{year or ''}"


def invert_abstract(inverted: dict | None) -> str | None:
    """OpenAlex stores abstracts as an inverted index; rebuild the text."""
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort(key=lambda p: p[0])
    return " ".join(w for _, w in positions)


def strip_jats(text: str | None) -> str | None:
    """Crossref abstracts arrive as JATS XML; strip the tags."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# An inline tag - <i>, </sub>, <scp>, <jats:italic>, <mml:math ...> - which
# titles carry for species names and formulas. Only these: "x < 5" and
# "A<B on C>D" are text.
_INLINE_TAG = re.compile(
    r"</?(?:[a-z][\w.-]*:[\w.-]+|i|b|u|em|strong|sub|sup|sc|scp|span|font|"
    r"small|bold|italic|br|p|inline-formula|tex-math|math|mi|mo|mn|mrow|msub|msup)"
    r"(?:\s[^<>]*)?/?>")


def clean_text(text: str | None) -> str:
    """A title or journal name as plain text: markup dropped, entities
    decoded (Crossref writes "R&amp;D"), whitespace collapsed."""
    if not text:
        return ""
    text = html.unescape(_INLINE_TAG.sub("", str(text)))
    return " ".join(text.split())


def _year(value) -> int | None:
    """A plausible year from a source's field, or None."""
    if isinstance(value, bool):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 2100 else None


def _part(value, limit: int = 40) -> str | None:
    """A volume or issue as a short clean string, or None."""
    if value is None or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


# Hyphen-minus and its look-alikes - hyphen, non-breaking hyphen, figure,
# en and em dash, minus sign - with any spaces around them.
_DASH_RUN = re.compile(r"\s*[-\u2010-\u2015\u2212]+\s*")


def clean_pages(raw) -> str | None:
    """Pages as one canonical string: '46 - 66', '46–66' and '46--66' all
    become '46-66'; a range that starts and ends on one page is that page."""
    text = _part(raw, 200)
    if not text:
        return None
    text = _DASH_RUN.sub("-", text).strip("-")
    first, dash, last = text.partition("-")
    if dash and first == last:
        text = first
    return text[:40] or None


def page_range(first, last=None) -> str | None:
    """'46-66' from a first and a last page; the first alone when that is all
    there is, or when both are the same (an article number, usually)."""
    first, last = clean_pages(first), clean_pages(last)
    if not first:
        return None
    if last and last != first and "-" not in first:
        return clean_pages(f"{first}-{last}")
    return first


def _date_year(holder, field: str) -> int | None:
    """The year of one of Crossref's {"date-parts": [[2022, 1]]} dates."""
    date = holder.get(field) if isinstance(holder, dict) else None
    parts = (date or {}).get("date-parts") if isinstance(date, dict) else None
    first = parts[0] if parts else None
    return _year(first[0]) if first else None


def crossref_years(item: dict) -> tuple[int | None, int | None]:
    """(year, online_year) of a Crossref work.

    year is the version of record's: published-print, then the print date of
    the journal issue that carries it, then issued (the earliest of print and
    online). online_year is the earliest of all its publication dates.
    """
    issue = item.get("journal-issue") or {}
    dates = [_date_year(item, f) for f in ("published-print", "published-online",
                                           "issued", "published", "posted")]
    dates += [_date_year(issue, f) for f in ("published-print", "published-online")]
    earliest = min((y for y in dates if y), default=None)
    year = (_date_year(item, "published-print") or _date_year(issue, "published-print")
            or _date_year(item, "issued") or earliest)
    return year, earliest


def from_openalex(work: dict) -> dict:
    authorships = work.get("authorships") or []
    authors = []
    for a in authorships:
        name = (a.get("author") or {}).get("display_name")
        if name:
            authors.append(name)
    # The earliest date OpenAlex knows - online first, or the day the DOI was
    # registered - not the issue's year (see the module docstring).
    year = _year(work.get("publication_year"))
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    venue = clean_text(source.get("display_name")) or None
    issn_l = source.get("issn_l")
    issns = [i for i in (source.get("issn") or []) if i]
    best_oa = work.get("best_oa_location") or {}
    oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")
    openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
    biblio = work.get("biblio") or {}
    return {
        "doi": clean_doi(work.get("doi")),
        "title": clean_text(work.get("display_name") or work.get("title")),
        "abstract": invert_abstract(work.get("abstract_inverted_index")),
        "year": year,
        "online_year": year,
        "venue": venue,
        "volume": _part(biblio.get("volume")),
        "issue": _part(biblio.get("issue")),
        "pages": page_range(biblio.get("first_page"), biblio.get("last_page")),
        "issn_l": issn_l,
        "issns": issns,
        "work_type": work.get("type"),
        "authors": authors,
        "citation_count": work.get("cited_by_count"),
        "url": loc.get("landing_page_url") or work.get("id"),
        "oa_url": oa_url,
        "source": "openalex",
        "source_id": openalex_id,
    }


def from_semantic_scholar(paper: dict) -> dict:
    ext = paper.get("externalIds") or {}
    oa = paper.get("openAccessPdf") or {}
    venue = clean_text(paper.get("venue")) or None
    pub_venue = paper.get("publicationVenue") or {}
    issn = pub_venue.get("issn")
    types = paper.get("publicationTypes") or []
    year = _year(paper.get("year"))
    # {"name": "...", "volume": "50", "pages": "46 - 66"}. An arXiv paper's
    # "volume" is its identifier (abs/2106.15928), which is not one.
    journal = paper.get("journal") or {}
    volume = _part(journal.get("volume"))
    if volume and volume.lower().startswith("abs/"):
        volume = None
    return {
        "doi": clean_doi(ext.get("DOI")),
        "title": clean_text(paper.get("title")),
        "abstract": paper.get("abstract"),
        "year": year,
        "online_year": year,
        "venue": venue,
        "volume": volume,
        "issue": None,  # Semantic Scholar does not give the issue
        "pages": clean_pages(journal.get("pages")),
        "issn_l": issn,
        "issns": [issn] if issn else [],
        "work_type": _s2_work_type(types),
        "authors": [a.get("name") for a in (paper.get("authors") or []) if a.get("name")],
        "citation_count": paper.get("citationCount"),
        "url": paper.get("url"),
        "oa_url": oa.get("url"),
        "source": "semantic_scholar",
        "source_id": paper.get("paperId") or "",
    }


def from_crossref(item: dict) -> dict:
    authors = []
    for a in item.get("author") or []:
        given = " ".join((a.get("given") or "").split())
        family = " ".join((a.get("family") or "").split())
        if family and given and " " in family:
            # Crossref says which part is the family name. When it is more
            # than one word ("Lew Chuk Wai"), "Given Family" would lose that
            # - the last word would be read as the surname - so it is kept
            # in the unambiguous "Family, Given" form.
            name = f"{family}, {given}"
        else:
            name = " ".join(x for x in [given, family] if x)
        if not name and a.get("name"):
            name = " ".join(str(a["name"]).split())  # an organization
        if name:
            authors.append(name)
    year, online_year = crossref_years(item)
    titles = item.get("title") or []
    container = item.get("container-title") or []
    issns = [i for i in (item.get("ISSN") or []) if i]
    return {
        "doi": clean_doi(item.get("DOI")),
        "title": clean_text(titles[0] if titles else ""),
        "abstract": strip_jats(item.get("abstract")),
        "year": year,
        "online_year": online_year,
        "venue": clean_text(container[0] if container else "") or None,
        "volume": _part(item.get("volume")),
        "issue": _part(item.get("issue") or (item.get("journal-issue") or {}).get("issue")),
        # Elsevier puts an article number in "page" as well as in
        # "article-number"; others only in the latter.
        "pages": clean_pages(item.get("page")) or _part(item.get("article-number")),
        "issn_l": issns[0] if issns else None,
        "issns": issns,
        "work_type": _crossref_work_type(item.get("type")),
        "authors": authors,
        "citation_count": item.get("is-referenced-by-count"),
        "url": item.get("URL"),
        "oa_url": None,
        "source": "crossref",
        "source_id": clean_doi(item.get("DOI")) or "",
    }
