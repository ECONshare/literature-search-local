"""The app's own background work: searches, downloads, text extraction.

A small thread pool, because that work is network-bound and the app is one
process. Every job must be safe to run again from the start: the app can be
closed at any moment, and at the next start research.resume_all() re-submits
whatever was in the middle of a step.

Tests switch the pool to inline mode, so a submitted job runs to completion
before submit() returns and a test can drive the whole pipeline in order.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_WORKERS = 3

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_inline = False
_active = 0


def set_inline(inline: bool) -> None:
    """Run jobs on the caller's thread (tests) or in the pool (the app)."""
    global _inline
    _inline = inline


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                           thread_name_prefix="litreview-worker")
        return _executor


def _run(fn, args, kwargs) -> None:
    global _active
    with _lock:
        _active += 1
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Background job %s failed", getattr(fn, "__name__", fn))
    finally:
        with _lock:
            _active -= 1


def submit(fn, *args, **kwargs) -> None:
    """Run fn(*args, **kwargs) in the background, logging any failure."""
    if _inline:
        _run(fn, args, kwargs)
        return
    with _lock:
        global _active
        _active += 1  # counted from submission, so busy() sees queued jobs too

    def job():
        global _active
        with _lock:
            _active -= 1
        _run(fn, args, kwargs)

    _pool().submit(job)


def busy() -> int:
    """How many jobs are queued or running right now."""
    with _lock:
        return _active


def shutdown() -> None:
    """Stop accepting work. Running jobs are abandoned with the process; each
    is resumable, so nothing is lost."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------ named jobs
# A job submitted with submit_as says what it is doing - "downloading 2
# PDFs" - and in which project, so a page can show that instead of "finishing
# background work".
_labels: dict[int, tuple[str, int | None]] = {}
_next_label = 0


def submit_as(fn, *args, label: str, project_id: int | None = None, **kwargs) -> None:
    """submit(), with what the job is doing (and in which project) shown by
    labels() until it has finished."""
    global _next_label
    if _inline:
        _run(fn, args, kwargs)
        return
    with _lock:
        _next_label += 1
        key = _next_label
        _labels[key] = (label, project_id)

    def named(*a, **k):
        try:
            fn(*a, **k)
        finally:
            with _lock:
                _labels.pop(key, None)

    named.__name__ = getattr(fn, "__name__", "job")
    submit(named, *args, **kwargs)


def labels(project_id: int | None = None) -> list[str]:
    """What the named jobs waiting or running now are doing - in one
    project, with project_id (jobs of no project count everywhere)."""
    with _lock:
        return [label for label, pid in _labels.values()
                if project_id is None or pid is None or pid == project_id]


def unnamed() -> int:
    """Jobs waiting or running that do not say what they are doing."""
    with _lock:
        return max(0, _active - len(_labels))
