"""OpenAlex works search. Docs: https://docs.openalex.org/"""
import time
from urllib.parse import quote

import httpx

from ..config import settings
from . import normalize
from .http import PAGE_DELAY_SECONDS, get_json, make_client

BASE_URL = "https://api.openalex.org/works"
PER_PAGE = 50

# OpenAlex's search= parameter takes boolean queries: uppercase AND/OR/NOT,
# parentheses for grouping, double quotes for exact phrases — the docs' own
# example is search=(elmo AND "sesame street") NOT (cookie OR monster).
# That is exactly the canonical form boolean_string produces, so concept
# groups compile 1:1. (help.openalex.org/api/searching, checked 2026-08-24.)
boolean_query = normalize.boolean_string


def search(query: str, *, max_results: int = 100, year_from: int | None = None,
           year_to: int | None = None, client: httpx.Client | None = None) -> list[dict]:
    c = make_client(client)
    owns_client = client is None
    records: list[dict] = []
    try:
        filters = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        cursor = "*"
        while cursor and len(records) < max_results:
            params = {
                "search": query,
                "per-page": min(PER_PAGE, max_results - len(records)),
                "cursor": cursor,
            }
            if filters:
                params["filter"] = ",".join(filters)
            if settings.contact_email:
                params["mailto"] = settings.contact_email
            data = get_json(c, BASE_URL, params)
            for work in data.get("results", []):
                records.append(normalize.from_openalex(work))
            cursor = (data.get("meta") or {}).get("next_cursor")
            if cursor and len(records) < max_results:
                time.sleep(PAGE_DELAY_SECONDS)
    finally:
        if owns_client:
            c.close()
    return records[:max_results]


def get_by_doi(doi: str, *, client: httpx.Client | None = None) -> dict | None:
    """The one work with this DOI, or None if OpenAlex has no such work.

    OpenAlex registers DataCite DOIs as well as Crossref's, so an arXiv
    preprint's 10.48550/arXiv... DOI resolves here when it does nowhere else.
    """
    c = make_client(client)
    owns_client = client is None
    try:
        params = {"mailto": settings.contact_email} if settings.contact_email else {}
        work = get_json(c, f"{BASE_URL}/doi:{quote(doi, safe='/')}", params,
                        missing_ok=True)
    finally:
        if owns_client:
            c.close()
    return normalize.from_openalex(work) if work else None
