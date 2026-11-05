"""Per-paper locks, so parallel pipeline runs cannot collide on one file.

Several research questions can run at once, and they routinely select the same
paper: relevance is per question, but the PDF on disk is per paper. Two runs
reaching the same paper would otherwise both download it to
`original/{id}.pdf`, and - worse - both write `annotated/{id}.pdf` and the
cached Document Intelligence layout at the same time, which does not produce
"one of the two" but a torn file.

The runs are threads of one process, so an in-process lock is enough. It is
held only around the file work, never across a whole pipeline: two questions
reading the same paper wait for each other for the length of one annotation,
not for the length of a run.
"""
import threading

_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def for_key(key: str) -> threading.Lock:
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def for_paper(paper_id: int) -> threading.Lock:
    """The lock guarding one paper's files. Same object for the same id."""
    return for_key(f"paper:{paper_id}")
