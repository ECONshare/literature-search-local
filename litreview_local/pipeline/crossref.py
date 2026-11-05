"""Crossref works search. Docs: https://api.crossref.org/"""
import time
from urllib.parse import quote

import httpx

from ..config import settings
from . import normalize
from .http import PAGE_DELAY_SECONDS, get_json, make_client

BASE_URL = "https://api.crossref.org/works"
PAGE_SIZE = 50


def search(query: str, *, max_results: int = 100, year_from: int | None = None,
           year_to: int | None = None, client: httpx.Client | None = None) -> list[dict]:
    c = make_client(client)
    owns_client = client is None
    records: list[dict] = []
    try:
        filters = ["type:journal-article"]
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        offset = 0
        while len(records) < max_results:
            params = {
                "query": query,
                "rows": min(PAGE_SIZE, max_results - len(records)),
                "offset": offset,
                "filter": ",".join(filters),
            }
            if settings.contact_email:
                params["mailto"] = settings.contact_email
            data = get_json(c, BASE_URL, params)
            items = (data.get("message") or {}).get("items") or []
            if not items:
                break
            for item in items:
                records.append(normalize.from_crossref(item))
            offset += len(items)
            if len(records) < max_results:
                time.sleep(PAGE_DELAY_SECONDS)
    finally:
        if owns_client:
            c.close()
    return records[:max_results]


def get_by_doi(doi: str, *, client: httpx.Client | None = None) -> dict | None:
    """The one work with this DOI, or None if Crossref did not register it."""
    c = make_client(client)
    owns_client = client is None
    try:
        params = {"mailto": settings.contact_email} if settings.contact_email else {}
        data = get_json(c, f"{BASE_URL}/{quote(doi, safe='/')}", params,
                        missing_ok=True)
    finally:
        if owns_client:
            c.close()
    item = (data or {}).get("message")
    return normalize.from_crossref(item) if item else None
