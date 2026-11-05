"""Routes for the evidence export and the citation check.

Kept apart from web.py (the pages) so the two can change independently:
the pages link here - "Export with evidence", "Ask Claude to check the
citations" - and these answer with a file or a redirect back.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import evidence_export, models, research, tasks
from .config import settings
from .db import get_db

router = APIRouter()


def _question(db: Session, question_id: int) -> models.ResearchQuestion:
    rq = db.get(models.ResearchQuestion, question_id)
    if rq is None:
        raise HTTPException(status_code=404, detail="No such research question")
    return rq


def _filename(rq: models.ResearchQuestion, ext: str) -> str:
    stem = models.slugify(rq.headline or rq.text)[:60] or "answer"
    return f"{rq.label}-{stem}-with-evidence.{ext}"


@router.get("/questions/{question_id}/export/evidence.md")
def evidence_markdown(question_id: int, db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    doc = evidence_export.build(db, rq, settings.base_url)
    return Response(evidence_export.to_markdown(doc), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{_filename(rq, "md")}"'})


@router.get("/questions/{question_id}/export/evidence.html", response_class=HTMLResponse)
def evidence_html(question_id: int, download: int = 0, db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    doc = evidence_export.build(db, rq, settings.base_url)
    headers = ({"Content-Disposition": f'attachment; filename="{_filename(rq, "html")}"'}
               if download else {})
    return HTMLResponse(evidence_export.to_html(doc), headers=headers)


@router.post("/questions/{question_id}/verify")
def verify(question_id: int, db: Session = Depends(get_db)):
    """Ask Claude to check the answer's citations (a verify_answer task)."""
    rq = _question(db, question_id)
    research.request_verification(db, rq)
    tasks.commit(db)
    return RedirectResponse(f"/questions/{rq.id}#verification", status_code=303)
