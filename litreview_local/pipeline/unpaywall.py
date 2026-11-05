"""Unpaywall: find legal open-access copies for a DOI. Docs: https://unpaywall.org/products/api"""
import httpx

from ..config import settings
from .http import make_client

BASE_URL = "https://api.unpaywall.org/v2"


def best_oa_url(doi: str, *, client: httpx.Client | None = None) -> str | None:
    """Return the best open-access URL for a DOI, or None."""
    if not doi or not settings.contact_email:
        return None
    c = make_client(client)
    owns_client = client is None
    try:
        resp = c.get(f"{BASE_URL}/{doi}", params={"email": settings.contact_email})
        if resp.status_code != 200:
            return None
        data = resp.json()
        loc = data.get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url")
    except httpx.HTTPError:
        return None
    finally:
        if owns_client:
            c.close()
