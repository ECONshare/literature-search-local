"""Journal quality signals: the Norwegian Register, with OpenAlex as backup.

Why two sources, in this order:

The Norwegian Register (Kanalregisteret, run by HK-dir) grades publication
channels 0/1/2, where level 2 is reserved for roughly the top 20% *within a
field*. That per-field normalization is the whole point: raw citation rates
are not comparable across subfields - the Journal of Econometrics sits around
2.9 two-year mean citedness against the Quarterly Journal of Economics at
20.5, and a review journal outscores both simply because reviews attract
citations. A curated, field-normalized level avoids reading those gaps as
quality differences. It is also CC BY 4.0 / NLOD, so it can be redistributed.

OpenAlex fills the gaps. Its `is_core` flag (the CWTS Leiden core list) is
used as a coarse "recognized venue" signal rather than its citation metrics,
for the same reason: those metrics are kept as a tiebreaker inside a tier,
never as the thing that sets the tier.

Deliberately NOT ranked: anything without an ISSN. In economics that is books
and working papers - NBER/IZA preprints and monographs are where much of the
field's important work appears first, and a venue score would bury them. They
carry no tier and compete on citations instead.
"""
import csv
import io
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import locks, models
from .config import settings
from .pipeline.http import USER_AGENT

logger = logging.getLogger(__name__)

NORWEGIAN_CSV_URL = ("https://kanalregister.hkdir.no/publiseringskanaler/"
                     "csvliste/tidsskrift?request_locale=en")
DOWNLOAD_TIMEOUT = 180.0

# Tiers. Kept coarse on purpose: the difference between two journals a few
# citations apart is noise, the difference between a level-2 journal and an
# unlisted one is not.
TIER_TOP = 3        # Norwegian level 2 - top ~20% of its field
TIER_APPROVED = 2   # Norwegian level 1, or on the Leiden core list
TIER_LISTED = 1     # known to the register but not approved (level 0 / X)
TIER_UNKNOWN = None  # no ISSN, or no entry anywhere - never a penalty

# What an unrated item counts as when sorting, so books and working papers
# are neither rewarded nor punished for lacking a journal grade.
NEUTRAL_TIER = TIER_APPROVED

TIER_LABELS = {
    TIER_TOP: "level 2",
    TIER_APPROVED: "level 1",
    TIER_LISTED: "listed",
}


def normalize_issn(value: str | None) -> str | None:
    """Uppercase, hyphenated 8-character ISSN, or None."""
    if not value:
        return None
    raw = "".join(ch for ch in str(value).strip().upper()
                  if ch.isdigit() or ch == "X")
    if len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:]}"


def _current_level(row: dict, level_columns: list[str]) -> int | None:
    """Most recent published level for a journal.

    The register carries one column per year (newest first, and the newest
    are often blank because future years are not graded yet), so the current
    grade is the first column that actually has a value. Non-numeric codes
    (an "X" marks a channel under review) are treated as unknown rather than
    as a zero, which would read as a quality judgement the register did not
    make.
    """
    for column in level_columns:
        value = (row.get(column) or "").strip()
        if value.isdigit():
            return int(value)
        if value:
            return None  # graded, but not on the numeric scale
    return None


def load_norwegian_register(db: Session, *, url: str = NORWEGIAN_CSV_URL,
                            client: httpx.Client | None = None) -> int:
    """Download the register and upsert one row per ISSN. Returns rows written.

    A journal usually has both a print and an online ISSN; both are stored
    pointing at the same level, because papers cite whichever one their
    source happened to record.
    """
    owns = client is None
    c = client or httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                               headers={"User-Agent": USER_AGENT})
    try:
        resp = c.get(url)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
    finally:
        if owns:
            c.close()

    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fieldnames = reader.fieldnames or []
    level_columns = [f for f in fieldnames if f.startswith("Level ")]
    if not level_columns:
        raise RuntimeError("Norwegian register CSV has no Level columns")

    existing = {j.issn: j for j in db.execute(select(models.Journal)).scalars()}
    written = 0
    for row in reader:
        level = _current_level(row, level_columns)
        title = (row.get("International Title")
                 or row.get("Original Title") or "").strip()
        field = (row.get("NPI Scientific Field") or "").strip() or None
        for column in ("Print ISSN", "Online ISSN"):
            issn = normalize_issn(row.get(column))
            if not issn:
                continue
            journal = existing.get(issn)
            if journal is None:
                journal = models.Journal(issn=issn)
                db.add(journal)
                existing[issn] = journal
            journal.norwegian_level = level
            journal.npi_field = field
            if title:
                journal.title = title
            written += 1
    db.commit()
    logger.info("Norwegian register: %d ISSN rows loaded", written)
    return written


def register_loaded(db: Session) -> bool:
    return db.execute(
        select(models.Journal).where(models.Journal.norwegian_level.isnot(None))
        .limit(1)
    ).scalars().first() is not None


def fetch_openalex_metrics(issn: str,
                           client: httpx.Client | None = None) -> dict | None:
    """Journal metrics for one ISSN from OpenAlex.

    A single-entity lookup, which OpenAlex serves free and unmetered (list
    and search calls are the ones that draw on the daily budget), so this can
    run for every venue without a key.
    """
    owns = client is None
    c = client or httpx.Client(timeout=30.0, follow_redirects=True,
                               headers={"User-Agent": USER_AGENT})
    try:
        params = {}
        if settings.contact_email:
            params["mailto"] = settings.contact_email
        resp = c.get(f"https://api.openalex.org/sources/issn:{issn}",
                     params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OpenAlex source lookup failed for %s: %s", issn, exc)
        return None
    finally:
        if owns:
            c.close()
    stats = data.get("summary_stats") or {}
    return {
        "title": data.get("display_name"),
        "openalex_id": data.get("id"),
        "two_yr_citedness": stats.get("2yr_mean_citedness"),
        "h_index": stats.get("h_index"),
        "is_core": bool(data.get("is_core")),
    }


def _apply_metrics(journal: models.Journal, metrics: dict | None) -> None:
    journal.openalex_checked = True
    if metrics:
        journal.title = journal.title or metrics["title"]
        journal.openalex_id = metrics["openalex_id"]
        journal.two_yr_citedness = metrics["two_yr_citedness"]
        journal.h_index = metrics["h_index"]
        journal.is_core = metrics["is_core"]


def get_or_fetch(db: Session, issns: list[str],
                 client: httpx.Client | None = None) -> models.Journal | None:
    """The Journal row for the first ISSN that resolves.

    A journal already known from the register is used as-is, topped up with
    OpenAlex metrics the first time it is seen. Otherwise each ISSN is tried
    against OpenAlex in turn - a paper often reports the print ISSN when the
    register or OpenAlex knows the online one.

    A miss is cached too: without that, an ISSN neither source knows would be
    looked up again on every future search, forever.
    """
    candidates = [i for i in (normalize_issn(raw) for raw in issns) if i]
    if not candidates:
        return None

    # Everything below is check-then-insert against a table keyed on a
    # globally unique ISSN, with an OpenAlex call sitting in the middle of the
    # window - so two parallel runs sharing a venue would both miss and both
    # insert, and the loser's IntegrityError would leave its session needing a
    # rollback, silently costing the run every lookup after it. The lock is on
    # the ISSN, so runs looking at different journals never wait for each
    # other, and it is taken in a fixed order so two runs holding one each
    # cannot deadlock.
    with locks.for_key(f"issn:{sorted(candidates)[0]}"):
        return _resolve(db, candidates, client)


def _resolve(db: Session, candidates: list[str],
             client: httpx.Client | None) -> models.Journal | None:
    known = {j.issn: j for j in db.execute(
        select(models.Journal).where(models.Journal.issn.in_(candidates))
    ).scalars().all()}

    # Prefer a row that already carries a register level.
    for issn in candidates:
        journal = known.get(issn)
        if journal is None:
            continue
        if not journal.openalex_checked:
            _apply_metrics(journal, fetch_openalex_metrics(issn, client=client))
            db.commit()
        return journal

    for issn in candidates:
        metrics = fetch_openalex_metrics(issn, client=client)
        if metrics is None:
            continue
        journal = models.Journal(issn=issn)
        _apply_metrics(journal, metrics)
        return _insert(db, journal)

    # Nothing anywhere: remember the miss so it is not retried every search.
    return _insert(db, models.Journal(issn=candidates[0], openalex_checked=True))


def _insert(db: Session, journal: models.Journal) -> models.Journal | None:
    """Commit a new Journal, yielding to whoever got there first.

    The per-ISSN lock covers the runs in this process; a row can still arrive
    from somewhere else (the register loader, a second process), and losing
    that race must cost the caller its lookup, not its session.
    """
    issn = journal.issn
    db.add(journal)
    try:
        db.commit()
        return journal
    except IntegrityError:
        db.rollback()
        logger.info("Journal %s was inserted concurrently - using that row", issn)
        return db.execute(
            select(models.Journal).where(models.Journal.issn == issn)
        ).scalars().first()


def tier(journal: models.Journal | None) -> int | None:
    """Quality tier, Norwegian level first, Leiden core as the backup.

    Citation metrics deliberately do not set the tier - they only break ties
    inside one (see the module docstring).
    """
    if journal is None:
        return TIER_UNKNOWN
    if journal.norwegian_level == 2:
        return TIER_TOP
    if journal.norwegian_level == 1:
        return TIER_APPROVED
    if journal.norwegian_level == 0:
        return TIER_LISTED
    if journal.is_core:
        return TIER_APPROVED
    if journal.openalex_id:
        return TIER_LISTED
    return TIER_UNKNOWN


def tier_label(value: int | None) -> str:
    return TIER_LABELS.get(value, "unrated")
