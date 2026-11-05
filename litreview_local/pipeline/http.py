"""Shared HTTP helpers for the API clients."""
import time

import httpx

USER_AGENT = "litreview-local/0.1 (personal literature-review tool)"
DEFAULT_TIMEOUT = 30.0
PAGE_DELAY_SECONDS = 0.6  # gentle pacing between paginated requests


def make_client(client: httpx.Client | None = None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})


def get_json(client: httpx.Client, url: str, params: dict, headers: dict | None = None,
             retries: int = 3, *, missing_ok: bool = False) -> dict | None:
    """GET with basic retry on 429/5xx.

    missing_ok is for a lookup by identifier, where a 404 is the answer "no
    such record" rather than a failure: it returns None at once instead of
    being retried and raised.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(url, params=params, headers=headers or {})
            if missing_ok and resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:  # network errors, bad status
            last_exc = exc
            time.sleep(1.0 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Request to {url} failed after {retries} attempts (rate-limited)")
