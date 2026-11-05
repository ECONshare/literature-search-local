"""Merge and de-duplicate canonical records from multiple sources.

Strategy:
1. Primary key: DOI (cleaned, lowercase).
2. Fallback key: normalized title + year (for records without a DOI).
When two records collide, fields are merged: prefer non-empty values, prefer
records that carry an abstract, keep the max citation count, and union
source_ids.

The year is the exception to "first non-empty wins". Crossref's is the
version of record's (the issue's year, see normalize.py), so it replaces the
online-first year OpenAlex and Semantic Scholar report, and the earliest year
any of them gives is kept as online_year.
"""
from .normalize import title_key


def _key(record: dict) -> str:
    if record.get("doi"):
        return f"doi:{record['doi']}"
    return f"title:{title_key(record.get('title', ''), record.get('year'))}"


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for field in ("doi", "abstract", "venue", "url", "oa_url",
                  "volume", "issue", "pages"):
        if not merged.get(field) and extra.get(field):
            merged[field] = extra[field]
    if extra.get("title") and len(extra["title"]) > len(merged.get("title") or ""):
        merged["title"] = extra["title"]
    if not merged.get("authors") and extra.get("authors"):
        merged["authors"] = extra["authors"]
    if extra.get("year") and (not merged.get("year")
                              or extra.get("source") == "crossref"):
        merged["year"] = extra["year"]
    online = [y for y in (base.get("online_year"), extra.get("online_year")) if y]
    merged["online_year"] = min(online, default=None)
    cc_a, cc_b = merged.get("citation_count"), extra.get("citation_count")
    merged["citation_count"] = max([c for c in (cc_a, cc_b) if c is not None], default=None)
    ids = dict(merged.get("source_ids") or {})
    for rec in (base, extra):
        if rec.get("source") and rec.get("source_id"):
            ids.setdefault(rec["source"], rec["source_id"])
    merged["source_ids"] = ids
    # Raw relevance proxy: best (lowest) position within any source's results.
    ranks = [r for r in (base.get("raw_rank"), extra.get("raw_rank")) if r is not None]
    merged["raw_rank"] = min(ranks, default=None)
    return merged


def dedupe(records: list[dict]) -> list[dict]:
    """De-duplicate a list of canonical records, merging duplicates."""
    by_key: dict[str, dict] = {}
    doi_of_title_key: dict[str, str] = {}
    order: list[str] = []

    for rec in records:
        if not (rec.get("title") or rec.get("doi")):
            continue
        key = _key(rec)
        # Cross-link: a DOI record whose title matches an earlier DOI-less record.
        if rec.get("doi"):
            tkey = f"title:{title_key(rec.get('title', ''), rec.get('year'))}"
            if tkey in by_key and key not in by_key:
                # Upgrade the earlier title-keyed entry to this DOI key.
                by_key[key] = by_key.pop(tkey)
                order[order.index(tkey)] = key
                doi_of_title_key[tkey] = key
            elif tkey in doi_of_title_key:
                key = doi_of_title_key[tkey]
        else:
            tkey = _key(rec)
            if tkey in doi_of_title_key:
                key = doi_of_title_key[tkey]

        if key in by_key:
            by_key[key] = _merge(by_key[key], rec)
        else:
            by_key[key] = _merge(rec, {})  # normalizes source_ids
            order.append(key)

    return [by_key[k] for k in order]
