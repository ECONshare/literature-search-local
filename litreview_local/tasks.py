"""Claude's work queue.

Everything that needs a model is a ClaudeTask: planning a search, ranking
results, reading a paper, writing an answer, identifying an uploaded PDF,
transcribing a scanned page. The app writes tasks; the user's own Claude takes
them through the MCP server (next_task), does what each one says and hands the
result back (submit_task). The app then carries on by itself until it needs
Claude again.

A task kind (TaskKind, registered by the module that owns the step) supplies:

- the instructions and the output schema shown to Claude,
- build_input: what Claude needs to read, assembled when the task is handed
  out (so it reflects the library as it is then, not when it was queued),
- check: semantic checks beyond the schema (ids that exist, keys that exist),
- apply: what the app does with an accepted answer,
- still_needed: whether a queued task is still worth doing,
- on_dropped: what to do when a task is given up on, so the pipeline moves on.

Nothing is applied until a valid answer arrives, and a claim is only a lease:
a chat that closes mid-task lets the lease run out and the task goes back to
the queue. That is what makes the queue safe to leave for days.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger(__name__)

LEASE_MINUTES = 20
# Invalid answers to one task before it is given up on. Generous: each refusal
# says exactly what to fix, and a task given up on loses its step.
MAX_REFUSALS = 8
# How long next_task may block waiting for the app's background work to
# produce a task. Kept under the tool-call timeouts of the Claude apps.
MAX_WAIT_SECONDS = 50


class TaskError(Exception):
    """A refusal addressed to Claude: the message says what to do."""


@dataclass
class TaskKind:
    name: str
    title: str
    instructions: str
    schema: dict
    build_input: Callable[[Session, models.ClaudeTask], dict]
    apply: Callable[[Session, models.ClaudeTask, dict], dict]
    describe: Callable[[models.ClaudeTask], str] | None = None
    check: Callable[[Session, models.ClaudeTask, dict], list[str]] | None = None
    still_needed: Callable[[Session, models.ClaudeTask], bool] | None = None
    on_dropped: Callable[[Session, models.ClaudeTask], None] | None = None
    priority: int = 100


_KINDS: dict[str, TaskKind] = {}


def register(kind: TaskKind) -> TaskKind:
    _KINDS[kind.name] = kind
    return kind


def kind_of(name: str) -> TaskKind | None:
    return _KINDS.get(name)


def kinds() -> dict[str, TaskKind]:
    return dict(_KINDS)


# ------------------------------------------------------------ validation

def _is_type(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float)
                                          and value.is_integer())
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate(schema: dict, value: Any, path: str = "output",
             limit: int = 20) -> list[str]:
    """Check value against the JSON Schema subset the task schemas use:
    type (or a list of types), properties, required, items, enum. Extra keys
    are allowed - Claude adding a field is not worth a refusal."""
    errors: list[str] = []
    types = schema.get("type")
    if types:
        allowed = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in allowed):
            return [f"{path} should be {' or '.join(allowed)}, "
                    f"got {_type_name(value)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is missing")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{path}.{key}", limit))
                if len(errors) >= limit:
                    return errors[:limit]
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate(schema["items"], item, f"{path}[{i}]", limit))
            if len(errors) >= limit:
                return errors[:limit]
    return errors


# ---------------------------------------------------------- the queue

_wake = threading.Condition()
_claim_lock = threading.Lock()


def _notify() -> None:
    with _wake:
        _wake.notify_all()


def _match(column, value):
    return column.is_(None) if value is None else column == value


def create(db: Session, kind: str, *, project_id: int,
           research_question_id: int | None = None,
           paper_id: int | None = None, document_id: int | None = None,
           annotation_id: int | None = None, payload: dict | None = None,
           priority: int | None = None) -> models.ClaudeTask:
    """Queue a task, unless the same one is already waiting. Caller commits.

    "The same one" is the same kind, the same rows and the same payload: a
    pipeline step that runs twice (a resumed download, say) must not put the
    same paper in front of Claude twice.
    """
    payload = payload or {}
    for task in db.execute(
        select(models.ClaudeTask).where(
            models.ClaudeTask.kind == kind,
            models.ClaudeTask.status.in_(models.TASK_LIVE_STATUSES),
            models.ClaudeTask.project_id == project_id,
            _match(models.ClaudeTask.research_question_id, research_question_id),
            _match(models.ClaudeTask.paper_id, paper_id),
            _match(models.ClaudeTask.document_id, document_id),
            _match(models.ClaudeTask.annotation_id, annotation_id))
    ).scalars().all():
        if (task.payload or {}) == payload:
            return task
    registered = _KINDS.get(kind)
    task = models.ClaudeTask(
        kind=kind, project_id=project_id,
        research_question_id=research_question_id, paper_id=paper_id,
        document_id=document_id, annotation_id=annotation_id,
        payload=payload,
        priority=(priority if priority is not None
                  else registered.priority if registered else 100))
    db.add(task)
    db.flush()
    _notify()
    return task


def live_tasks(db: Session, **where) -> list[models.ClaudeTask]:
    """Waiting or claimed tasks matching column=value filters."""
    query = select(models.ClaudeTask).where(
        models.ClaudeTask.status.in_(models.TASK_LIVE_STATUSES))
    for name, value in where.items():
        if name == "kinds":
            query = query.where(models.ClaudeTask.kind.in_(value))
        else:
            query = query.where(_match(getattr(models.ClaudeTask, name), value))
    return db.execute(query.order_by(models.ClaudeTask.id)).scalars().all()


def cancel(db: Session, reason: str, **where) -> int:
    """Withdraw live tasks matching the filters (see live_tasks). The
    pipeline that queued them has moved on, so nothing is called back.
    Caller commits."""
    withdrawn = 0
    for task in live_tasks(db, **where):
        task.status = models.TASK_CANCELLED
        task.error = reason
        task.completed_at = models.utcnow()
        withdrawn += 1
    return withdrawn


def expire_leases(db: Session) -> int:
    """Put tasks whose lease has run out back in the queue."""
    now = models.utcnow()
    expired = 0
    for task in db.execute(
        select(models.ClaudeTask).where(
            models.ClaudeTask.status == models.TASK_CLAIMED)
    ).scalars().all():
        until = models.aware(task.lease_expires_at)
        if until is None or until <= now:
            task.status = models.TASK_OPEN
            task.claimed_by = None
            expired += 1
    if expired:
        db.commit()
    return expired


def claim_next(db: Session, *, kind: str | None = None,
               project_id: int | None = None,
               research_question_id: int | None = None,
               client: str | None = None) -> models.ClaudeTask | None:
    """Take the most urgent waiting task, or None."""
    with _claim_lock:
        expire_leases(db)
        query = select(models.ClaudeTask).where(
            models.ClaudeTask.status == models.TASK_OPEN)
        if kind:
            query = query.where(models.ClaudeTask.kind == kind)
        if project_id:
            query = query.where(models.ClaudeTask.project_id == project_id)
        if research_question_id:
            query = query.where(
                models.ClaudeTask.research_question_id == research_question_id)
        query = query.order_by(models.ClaudeTask.priority, models.ClaudeTask.id)
        for task in db.execute(query.limit(50)).scalars().all():
            registered = _KINDS.get(task.kind)
            if registered is None:
                continue
            if registered.still_needed and not registered.still_needed(db, task):
                task.status = models.TASK_CANCELLED
                task.error = "No longer needed"
                task.completed_at = models.utcnow()
                continue
            _lease(task, client)
            db.commit()
            return task
        db.commit()
        return None


def claim(db: Session, task_id: int, client: str | None = None) -> models.ClaudeTask:
    """Take one particular task (to read it again, or a task the user picked)."""
    with _claim_lock:
        expire_leases(db)
        task = db.get(models.ClaudeTask, task_id)
        if task is None:
            raise TaskError(f"There is no task {task_id}. Call next_task to get "
                            "one that is waiting.")
        if not task.live:
            raise TaskError(_not_live(task))
        _lease(task, client)
        db.commit()
        return task


def _lease(task: models.ClaudeTask, client: str | None) -> None:
    now = models.utcnow()
    task.status = models.TASK_CLAIMED
    task.claimed_at = now
    task.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
    task.claimed_by = (client or "")[:100] or None


def release(db: Session, task_id: int) -> models.ClaudeTask:
    """Hand a claimed task back to the queue."""
    task = db.get(models.ClaudeTask, task_id)
    if task is None:
        raise TaskError(f"There is no task {task_id}.")
    if task.status == models.TASK_CLAIMED:
        task.status = models.TASK_OPEN
        task.claimed_by = None
        task.lease_expires_at = None
        db.commit()
        _notify()
    return task


def drop(db: Session, task: models.ClaudeTask, reason: str) -> None:
    """Give up on a task and let its pipeline move on without it."""
    if not task.live:
        return
    task.status = models.TASK_FAILED
    task.error = reason
    task.completed_at = models.utcnow()
    db.flush()
    registered = _KINDS.get(task.kind)
    if registered and registered.on_dropped:
        registered.on_dropped(db, task)
    # commit(), not db.commit(): on_dropped may schedule the step that comes
    # next, and only commit() starts what after_commit queued.
    commit(db)


def _not_live(task: models.ClaudeTask) -> str:
    if task.status == models.TASK_DONE:
        return (f"Task {task.id} has already been done. Call next_task for "
                "the next one.")
    if task.status == models.TASK_CANCELLED:
        return (f"Task {task.id} is no longer needed ({task.error or 'withdrawn'}). "
                "Call next_task for the next one.")
    return (f"Task {task.id} was given up on ({task.error or 'failed'}). Call "
            "next_task for the next one.")


# -------------------------------------------------------- render / submit

def describe(task: models.ClaudeTask) -> str:
    registered = _KINDS.get(task.kind)
    if registered and registered.describe:
        try:
            return registered.describe(task)
        except Exception:  # a label must never break a listing
            logger.exception("Could not describe task %s", task.id)
    return registered.title if registered else task.kind


def render(db: Session, task: models.ClaudeTask) -> dict:
    """Everything Claude needs to do one task."""
    registered = _KINDS.get(task.kind)
    if registered is None:
        raise TaskError(f"Task {task.id} is of an unknown kind ({task.kind}).")
    out: dict[str, Any] = {
        "task_id": task.id,
        "kind": task.kind,
        "title": registered.title,
        "about": describe(task),
        "project": {"id": task.project_id,
                    "name": task.project.name if task.project else None},
    }
    rq = task.research_question
    if rq is not None:
        out["research_question"] = {"question_id": rq.id, "label": rq.label,
                                    "text": rq.text}
    out["instructions"] = registered.instructions
    out["input"] = registered.build_input(db, task)
    out["output_schema"] = registered.schema
    out["how_to_submit"] = (
        f"Call submit_task with task_id={task.id} and output set to an object "
        "matching output_schema. The task is yours until "
        f"{models.aware(task.lease_expires_at).isoformat() if task.lease_expires_at else 'it is submitted'}; "
        "call release_task if you cannot finish it.")
    if task.error and task.attempts:
        out["previous_refusal"] = task.error
    return out


def submit(db: Session, task_id: int, output: Any,
           client: str | None = None) -> dict:
    """Validate and apply Claude's answer to one task.

    The task is marked done before the kind's apply runs (and flushed), so
    apply can ask "is anything else of this step still waiting?" and get the
    right answer. A refusal - invalid output, or a check that fails - leaves
    the task with Claude, with the reason recorded and the lease extended.
    """
    task = db.get(models.ClaudeTask, task_id)
    if task is None:
        raise TaskError(f"There is no task {task_id}. Call next_task to get one "
                        "that is waiting.")
    if not task.live:
        raise TaskError(_not_live(task))
    registered = _KINDS.get(task.kind)
    if registered is None:
        raise TaskError(f"Task {task.id} is of an unknown kind ({task.kind}).")

    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            raise TaskError("output must be a JSON object matching the task's "
                            "output_schema, not text.") from None

    errors = validate(registered.schema, output)
    if not errors and registered.check is not None:
        errors = registered.check(db, task, output)
    if errors:
        task.attempts = (task.attempts or 0) + 1
        task.error = "; ".join(errors)[:2000]
        if task.attempts >= MAX_REFUSALS:
            drop(db, task, f"Given up after {task.attempts} answers that could "
                           f"not be accepted. Last problem: {task.error}")
            raise TaskError(
                f"That answer could not be accepted either ({'; '.join(errors)}), "
                f"and task {task.id} has now been set aside so the rest of the "
                "work can go on. Call next_task for the next one.")
        if task.status == models.TASK_CLAIMED:
            task.lease_expires_at = models.utcnow() + timedelta(minutes=LEASE_MINUTES)
        db.commit()
        raise TaskError(
            "The answer was not accepted: " + "; ".join(errors) + ". Fix these "
            f"and call submit_task again with task_id={task.id}.")

    task.status = models.TASK_DONE
    task.result = output
    task.error = None
    task.completed_at = models.utcnow()
    if client and not task.claimed_by:
        task.claimed_by = client[:100]
    db.flush()
    after: list = db.info.setdefault("after_commit", [])
    try:
        outcome = registered.apply(db, task, output) or {}
        db.commit()
    except TaskError:
        db.rollback()
        db.info.pop("after_commit", None)
        raise
    except Exception as exc:
        logger.exception("Applying task %s failed", task_id)
        db.rollback()
        db.info.pop("after_commit", None)
        raise TaskError(f"The app could not use that answer ({exc}). It has "
                        "been logged; try submitting again, or call "
                        "release_task and move on.") from exc
    for fn, args in db.info.pop("after_commit", after):
        try:
            fn(*args)
        except Exception:
            logger.exception("Follow-up of task %s failed", task_id)
    _notify()
    return outcome


def after_commit(db: Session, fn, *args) -> None:
    """Run fn(*args) once the current transaction has been committed - how a
    step starts background work that must see what it just wrote. submit()
    runs these itself; any other caller commits with tasks.commit(db)."""
    db.info.setdefault("after_commit", []).append((fn, args))


def commit(db: Session) -> None:
    """Commit, then run whatever after_commit queued on this session."""
    db.commit()
    for fn, args in db.info.pop("after_commit", []):
        try:
            fn(*args)
        except Exception:
            logger.exception("Follow-up %s failed", getattr(fn, "__name__", fn))
    _notify()


# -------------------------------------------------------------- overview

def pending_counts(db: Session, project_id: int | None = None) -> dict:
    """{"total", "open", "claimed", "by_kind": {kind: n}} of live tasks."""
    query = (select(models.ClaudeTask.kind, models.ClaudeTask.status,
                    func.count(models.ClaudeTask.id))
             .where(models.ClaudeTask.status.in_(models.TASK_LIVE_STATUSES))
             .group_by(models.ClaudeTask.kind, models.ClaudeTask.status))
    if project_id:
        query = query.where(models.ClaudeTask.project_id == project_id)
    counts = {"total": 0, "open": 0, "claimed": 0, "by_kind": {}}
    for kind, status, n in db.execute(query).all():
        counts["total"] += n
        counts["open" if status == models.TASK_OPEN else "claimed"] += n
        counts["by_kind"][kind] = counts["by_kind"].get(kind, 0) + n
    return counts


def background_activity(db: Session, project_id: int | None = None) -> list[str]:
    """What the app itself is doing right now - work that will produce tasks
    for Claude when it finishes."""
    from . import worker

    lines = []
    query = select(models.ResearchQuestion).where(
        models.ResearchQuestion.status.in_(models.RQ_APP_STATUSES))
    if project_id:
        query = query.where(models.ResearchQuestion.project_id == project_id)
    for rq in db.execute(query).scalars().all():
        lines.append(f"{rq.label} (question_id {rq.id}): "
                     f"{rq.status_detail or rq.status_label}")
    query = select(models.PaperIdentification).where(
        models.PaperIdentification.status.in_(
            [models.IDENT_QUEUED, models.IDENT_PARSING, models.IDENT_SEARCHING]))
    idents = db.execute(query).scalars().all()
    if project_id:
        idents = [i for i in idents if i.paper and i.paper.project_id == project_id]
    if idents:
        lines.append(f"identifying {len(idents)} uploaded PDF"
                     f"{'' if len(idents) == 1 else 's'}")
    query = select(func.count(models.ProjectDocument.id)).where(
        models.ProjectDocument.status == models.DOC_PENDING)
    if project_id:
        query = query.where(models.ProjectDocument.project_id == project_id)
    docs = db.execute(query).scalar_one()
    if docs:
        lines.append(f"reading {docs} project document{'' if docs == 1 else 's'}")
    lines.extend(worker.labels(project_id))
    if not lines and worker.unnamed():
        lines.append("finishing background work")
    return lines


def wait_for_work(session_factory, *, project_id: int | None = None,
                  wait_seconds: float = 0) -> None:
    """Wait - without claiming anything - until a task is waiting or the
    app's background work is over, for at most wait_seconds."""
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0),
                                               MAX_WAIT_SECONDS))
    while True:
        db = session_factory()
        try:
            if pending_counts(db, project_id)["open"]:
                return
            activity = background_activity(db, project_id)
        finally:
            db.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not activity:
            return
        with _wake:
            _wake.wait(timeout=min(2.0, remaining))


def wait_and_claim(session_factory, *, kind: str | None = None,
                   project_id: int | None = None,
                   research_question_id: int | None = None,
                   client: str | None = None,
                   wait_seconds: float = 0) -> tuple[int | None, list[str]]:
    """Claim a task, waiting up to wait_seconds while the app's own
    background work may still produce one. Returns (task_id, activity) - the
    activity being what is still going on if no task turned up. Each poll
    uses a fresh session, so work committed by other threads is seen."""
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0),
                                               MAX_WAIT_SECONDS))
    while True:
        db = session_factory()
        try:
            task = claim_next(db, kind=kind, project_id=project_id,
                              research_question_id=research_question_id,
                              client=client)
            if task is not None:
                return task.id, []
            activity = background_activity(db, project_id)
        finally:
            db.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not activity:
            # The step that just ended may have queued its task in the same
            # commit that ended it: look once more before saying nothing came.
            db = session_factory()
            try:
                task = claim_next(db, kind=kind, project_id=project_id,
                                  research_question_id=research_question_id,
                                  client=client)
                if task is not None:
                    return task.id, []
            finally:
                db.close()
            return None, activity
        with _wake:
            _wake.wait(timeout=min(2.0, remaining))


# ---------------------------------------------------------- Claude seen

_SEEN_KEY = "claude_last_seen"
_seen_cache: dict[str, float] = {}


def note_client(db: Session, client: str | None, *, force: bool = False) -> None:
    """Remember that Claude called in, and from which app (throttled to one
    write a minute per app). Kept per app as well as overall, so the
    Connect page can tell Claude Desktop calling from Claude Code calling."""
    client = client or "Claude"
    now = time.monotonic()
    if not force and now - _seen_cache.get(client, 0.0) < 60:
        return
    _seen_cache[client] = now
    _seen_cache["at"] = now
    stamp = models.utcnow().isoformat()
    row = db.get(models.AppState, _SEEN_KEY)
    previous = dict(row.value or {}) if row is not None else {}
    clients = dict(previous.get("clients") or {})
    clients[client] = stamp
    value = {"at": stamp, "client": client, "clients": clients}
    if row is None:
        db.add(models.AppState(key=_SEEN_KEY, value=value))
    else:
        row.value = value
    db.commit()


def last_seen(db: Session) -> dict | None:
    row = db.get(models.AppState, _SEEN_KEY)
    return row.value if row is not None else None
