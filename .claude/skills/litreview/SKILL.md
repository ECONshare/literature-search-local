---
name: litreview
description: Work on the user's LitReview literature review through the litreview MCP server. Use it to work through the queue (plan searches, rank results, read papers, write cited answers, transcribe or parse PDFs, identify uploaded PDFs), to start research questions, and to draft text from answers with checkable citations. Use whenever the user mentions LitReview, their literature review or its queue, research questions (RQ1, RQ2...), parsing their PDFs or documents, or citing from their review.
---

# Working on LitReview

LitReview runs on this computer, at http://127.0.0.1:8765. It searches the
scholarly sources, downloads PDFs, keeps their text and highlights passages.
It has no model of its own: every step that needs judgment waits for you as
a **task** in its queue. The `litreview` MCP tools are how you reach it.

If the tools are missing or fail to connect, LitReview is not running. Ask
the user to start it by double-clicking `start-windows.bat` or
`start-mac.command` in the LitReview folder, then retry.

## Working through the queue

When the user says "work through my LitReview queue", "process my review",
"parse my documents" or similar:

1. Call `get_pending_work` to see what is waiting and what the app is doing.
2. Loop: call `next_task`, do exactly what its `instructions` say with its
   `input`, then `submit_task(task_id, output)` with an `output` shaped like
   its `output_schema`.
3. When `next_task` says the app is still working (searching or downloading),
   call it again with `wait_seconds=45`. Stop when it says nothing is waiting.
4. At the end, tell the user in two or three lines what was done. Don't
   narrate each task as you go.

Every task is self-contained, so you never need to remember an earlier one.
If `submit_task` refuses an answer, the message says exactly what to fix. Fix
it and submit again with the same `task_id`. If a task genuinely cannot be
done, call `skip_task` with the reason.

### Reading papers in parallel (Claude Code)

`annotate_paper` tasks carry a whole paper's text. Don't read them in the main
conversation. Hand them to the **litreview-reader** subagent, several at once
(about three or four), each told: "Take one annotate_paper task from
LitReview and do it." Each subagent claims its own task with
`next_task(kind="annotate_paper")`, so they never collide. Keep the other
kinds (planning, ranking, writing the answer) in the main conversation.
To wait for the app without taking a task, call
`get_pending_work(wait_seconds=45)`.

`parse_pdf` tasks give a `pdf_path`. Read those pages of the PDF with your
Read tool (`pages` parameter) rather than asking for page images.

## Quotes are verbatim

The app finds every quote in the PDF to highlight it. Copy quotes character
for character from the text in the task: no paraphrase, no tidying, no
ellipses joining separate sentences. Each passage then carries `located`:

- `pdf`: found and highlighted in the PDF.
- `text`: found in the paper's text with its page, but not highlightable.
- `none`: not found anywhere. Treat its page as unreliable.

### Checking an answer

Run `verify_answer`'s task in a fresh subagent, not in the conversation that
wrote the answer. A writer marking its own work is lenient.

### Usage

Reading is the expensive part. A question with six papers is roughly 25 to
40 reading tasks, which comes to several hundred thousand tokens of the
user's Claude allowance. Say so if the user is about to start many questions
at once.

## Writing from the review

- `get_answer(question_ids=[...])` gives each answer once with `[CIT:key]`
  markers, the passage behind every key, and each paper's APA reference.
- `get_highlights` gives the passages grouped by paper, with section and
  context. `search_paper_text` and `get_paper_text` read beyond them.
- A passage you found yourself becomes citable with
  `add_evidence(question_id, paper_id, quote, why)`. It is checked against
  the paper's text and refused, with the closest real sentence, if it isn't
  there.
- `save_answer` stores an edited answer (keep the `[CIT:key]` markers). It
  replaces the user's text, so only do it when they ask.
- Check `list_documents` first: a project's protocol or manuscript sets the
  rules the writing must follow.

## Starting work

- Before `ask_question`, call `list_questions`. A near-duplicate is refused,
  and an existing answer may already cover the question.
- You may pass the planned search yourself (`concept_groups`, `query`,
  `headline`) to skip the planning task.
- `find_article` + `include_articles` add a paper the search missed.
- `tag_papers` and `set_screening` change tags and include/exclude, when the
  user asks.
- `request_parse` queues a proper transcription of a scan, or of a PDF whose
  tables and equations matter. `list_pdf_texts` shows which PDFs need one.

Questions carry labels (RQ1, RQ2...) that the user sees. Use them in what you
write, and pass `question_id` to the tools.
