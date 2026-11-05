"""Free-form tags on articles: "method: DiD", "to read", "Chapter 2".

Per project, matched case-insensitively (models.tag_key), so a tag typed as
"To read" on one card and "to read" on another is one tag. A tag no article
carries any more is removed, so the project's tag list never fills with
leftovers.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models

MAX_TAGS_PER_CALL = 20


def get_or_create(db: Session, project_id: int, name: str) -> models.Tag | None:
    clean = models.clean_tag_name(name)
    if not clean:
        return None
    key = models.tag_key(clean)
    tag = db.execute(
        select(models.Tag).where(models.Tag.project_id == project_id,
                                 models.Tag.key == key)
    ).scalar_one_or_none()
    if tag is None:
        tag = models.Tag(project_id=project_id, name=clean, key=key)
        db.add(tag)
        db.flush()
    return tag


def add(db: Session, paper: models.Paper, names: list[str]) -> list[str]:
    """Put tags on an article. Returns the names actually added. Caller commits."""
    added = []
    for name in names[:MAX_TAGS_PER_CALL]:
        tag = get_or_create(db, paper.project_id, name)
        if tag is not None and tag not in paper.tags:
            paper.tags.append(tag)
            added.append(tag.name)
    return added


def remove(db: Session, paper: models.Paper, names: list[str]) -> list[str]:
    """Take tags off an article. Returns the names removed. Caller commits."""
    keys = {models.tag_key(n) for n in names}
    removed = [t for t in paper.tags if t.key in keys]
    for tag in removed:
        paper.tags.remove(tag)
    db.flush()
    prune(db, paper.project_id)
    return [t.name for t in removed]


def prune(db: Session, project_id: int) -> int:
    """Delete the project's tags that no article carries."""
    unused = db.execute(
        select(models.Tag).where(
            models.Tag.project_id == project_id,
            ~models.Tag.id.in_(select(models.paper_tags.c.tag_id)))
    ).scalars().all()
    for tag in unused:
        db.delete(tag)
    return len(unused)


def with_counts(db: Session, project_id: int) -> list[dict]:
    """[{"name", "count"}] for the project's tags, by name."""
    rows = db.execute(
        select(models.Tag.name, func.count(models.paper_tags.c.paper_id))
        .outerjoin(models.paper_tags, models.paper_tags.c.tag_id == models.Tag.id)
        .where(models.Tag.project_id == project_id)
        .group_by(models.Tag.id)
        .order_by(func.lower(models.Tag.name))
    ).all()
    return [{"name": name, "count": count} for name, count in rows]


def rename(db: Session, project_id: int, old: str, new: str) -> models.Tag | None:
    """Rename a tag; renaming onto an existing tag merges the two."""
    tag = db.execute(
        select(models.Tag).where(models.Tag.project_id == project_id,
                                 models.Tag.key == models.tag_key(old))
    ).scalar_one_or_none()
    clean = models.clean_tag_name(new)
    if tag is None or not clean:
        return None
    target = db.execute(
        select(models.Tag).where(models.Tag.project_id == project_id,
                                 models.Tag.key == models.tag_key(clean))
    ).scalar_one_or_none()
    if target is not None and target.id != tag.id:
        for paper in list(tag.papers):
            if target not in paper.tags:
                paper.tags.append(target)
        db.delete(tag)
        return target
    tag.name = clean
    tag.key = models.tag_key(clean)
    return tag
