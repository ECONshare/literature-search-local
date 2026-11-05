"""Semantic Scholar Graph API search. Docs: https://api.semanticscholar.org/"""
import time
from urllib.parse import quote

import httpx

from ..config import settings
from . import normalize
from .http import get_json, make_client

BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
# journal is {name, volume, pages}: what a reference needs beyond the venue.
FIELDS = ("title,abstract,year,venue,authors,citationCount,externalIds,"
          "openAccessPdf,url,publicationVenue,publicationTypes,journal")
PAGE_SIZE = 100
# S2 API keys allow 1 request/second cumulative; stay under that.
PAGE_DELAY_SECONDS = 1.1


def search(query: str, *, max_results: int = 100, year_from: int | None = None,
           year_to: int | None = None, client: httpx.Client | None = None) -> list[dict]:
    # The relevance endpoint takes plain text only — per the API docs: "No
    # special query syntax is supported" and "Hyphenated query terms yield
    # no matches (replace it with space to find matches)". Quotes and
    # hyphens would be matched literally (or not at all), so drop them.
    query = " ".join(query.replace('"', " ").replace("-", " ").split())
    c = make_client(client)
    owns_client = client is None
    records: list[dict] = []
    headers = {}
    if settings.s2_api_key:
        headers["x-api-key"] = settings.s2_api_key
    try:
        offset = 0
        while len(records) < max_results:
            params = {
                "query": query,
                "fields": FIELDS,
                "offset": offset,
                "limit": min(PAGE_SIZE, max_results - len(records)),
            }
            if year_from or year_to:
                params["year"] = f"{year_from or ''}-{year_to or ''}"
            data = get_json(c, BASE_URL, params, headers=headers)
            batch = data.get("data") or []
            if not batch:
                break
            for paper in batch:
                records.append(normalize.from_semantic_scholar(paper))
            if data.get("next") is None:
                break
            offset = data["next"]
            if len(records) < max_results:
                time.sleep(PAGE_DELAY_SECONDS)
    finally:
        if owns_client:
            c.close()
    return records[:max_results]


PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper"


def get_by_doi(doi: str, *, client: httpx.Client | None = None) -> dict | None:
    """The one paper with this DOI, or None if Semantic Scholar has none."""
    c = make_client(client)
    owns_client = client is None
    headers = {"x-api-key": settings.s2_api_key} if settings.s2_api_key else {}
    try:
        paper = get_json(c, f"{PAPER_URL}/DOI:{quote(doi, safe='/')}",
                         {"fields": FIELDS}, headers=headers, missing_ok=True)
    finally:
        if owns_client:
            c.close()
    return normalize.from_semantic_scholar(paper) if paper else None
