"""Claude's check of an answer's citations, and the export with evidence."""
import re

from litreview_local import pdfs
from litreview_local.pipeline import SOURCES

from test_pipeline_e2e import BODY, PAPERS, FakeSource, make_pdf, work_queue


def _answered(claude, monkeypatch):
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS))
    monkeypatch.setattr(pdfs, "download_pdf", lambda paper, client=None: (
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY), (True, None))[1])
    project = claude.call("create_project", name="Checked")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    keys = [h["key"] for p in claude.call(
        "get_highlights", question_id=started["question_id"])["papers"]
        for h in p["highlights"]]
    claude.call("save_answer", question_id=started["question_id"],
                text=(f"Remote work raised wellbeing. [CIT:{keys[0]}] "
                      f"It also cut costs for everyone. [CIT:{keys[1]}] [CIT:{keys[0]}]"))
    return started["question_id"], keys


def test_claude_checks_every_citation(claude, monkeypatch, client):
    question_id, keys = _answered(claude, monkeypatch)
    queued = claude.call("verify_answer", question_id=question_id)
    task = claude.call("next_task", kind="verify_answer")
    assert task["task_id"] == queued["queued"]
    sentences = task["input"]["sentences"]
    assert [s["keys"] for s in sentences] == [[keys[0]], [keys[1], keys[0]]]
    assert f"[CIT:{keys[0]}]" in task["input"]["evidence"]
    assert f'passage: "{BODY[0]}"' in task["input"]["evidence"]

    refused = claude.error("submit_task", task_id=task["task_id"], output={
        "checks": [{"sentence": 1, "key": keys[0], "verdict": "supported",
                    "note": ""}], "overall": "x"})
    assert "were not checked" in refused
    claude.call("submit_task", task_id=task["task_id"], output={
        "checks": [
            {"sentence": 1, "key": keys[0], "verdict": "supported", "note": ""},
            {"sentence": 2, "key": keys[1], "verdict": "unsupported",
             "note": "The passage says nothing about costs."},
            {"sentence": 2, "key": keys[0], "verdict": "partial",
             "note": "About wellbeing, not costs."}],
        "overall": "The cost claim has no support."})

    answer = claude.call("get_answer", question_id=question_id)["answers"][0]
    checks = {c["key"]: c["check"] for c in answer["citations"]}
    # A key keeps its weakest verdict across the sentences citing it.
    assert checks == {keys[0]: "partial", keys[1]: "unsupported"}
    assert answer["claude_check"]["overall"] == "The cost claim has no support."

    markdown = client.get(f"/questions/{question_id}/export/evidence.md")
    assert markdown.status_code == 200
    text = markdown.text
    assert "## Evidence" in text and BODY[0] in text
    assert "Check: unsupported - The passage says nothing about costs." in text
    # Citations merged and before the full stop in the manuscript text.
    assert re.search(r"wellbeing \([A-Z][a-z]+ & Second, 20\d\d\)\[1\]\.", text), text
    assert re.search(r"costs for everyone \([A-Z][a-z]+ & Second, 20\d\d; "
                     r"[A-Z][a-z]+ & Second, 20\d\d\)\[2,1\]\.", text), text
    page = client.get(f"/questions/{question_id}/export/evidence.html")
    assert page.status_code == 200 and "<sup>" in page.text

    # Editing the answer keeps the verdicts of the sentences left unchanged:
    # the first sentence is as it was, so its citation is still "supported".
    claude.call("save_answer", question_id=question_id,
                text=f"Remote work raised wellbeing. [CIT:{keys[0]}] "
                     f"A new claim. [CIT:{keys[1]}]")
    answer = claude.call("get_answer", question_id=question_id)["answers"][0]
    checks = {c["key"]: c.get("check") for c in answer["citations"]}
    assert checks == {keys[0]: "supported", keys[1]: None}
    assert answer["claude_check"]["overall"] == "The cost claim has no support."
    # Rewording that sentence too leaves nothing checked.
    claude.call("save_answer", question_id=question_id,
                text=f"Wellbeing rose with remote work. [CIT:{keys[0]}]")
    answer = claude.call("get_answer", question_id=question_id)["answers"][0]
    assert "check" not in answer["citations"][0]
    assert "claude_check" not in answer


def test_the_check_is_offered_from_the_page(claude, monkeypatch, client):
    question_id, _ = _answered(claude, monkeypatch)
    response = client.post(f"/questions/{question_id}/verify",
                           follow_redirects=False)
    assert response.status_code == 303
    pending = claude.call("get_pending_work")
    assert pending["by_kind"] == {"verify_answer": 1}


def test_added_evidence_is_in_the_next_rewrite(claude, monkeypatch):
    """A passage made citable with add_evidence is in the evidence the next
    writing task is given - a rewrite must not drop it."""
    question_id, keys = _answered(claude, monkeypatch)
    paper = claude.call("search_papers", question_id=question_id)["papers"][0]
    added = claude.call("add_evidence", question_id=question_id,
                        paper_id=paper["paper_id"], quote=BODY[2],
                        why="The cost side.")
    claude.call("rerun_question", question_id=question_id, mode="evidence")
    task = claude.call("next_task", wait_seconds=5)
    assert task["kind"] == "synthesize"
    assert f"[CIT:{added['citation_key']}]" in task["input"]["evidence"]
    assert BODY[2] in task["input"]["evidence"]
