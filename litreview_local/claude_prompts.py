"""What Claude is asked to do in each task, and the shape of its answer.

These are the prompts the server app sends to Azure OpenAI, rewritten as task
instructions: here the model is the user's own Claude, which reads a task
through the MCP server's next_task tool and answers it with submit_task. The
instructions travel with every task, so Claude needs no prior knowledge of
LitReview to do the work correctly.

Schemas are plain JSON Schema (the subset tasks.validate understands): they
are shown to Claude with the task and checked on submission.
"""

# ------------------------------------------------------------ parse_question

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "concept_groups": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
            "description": "Boolean structure of the search. Outer list: the "
                           "distinct concepts results must contain (ANDed). "
                           "Inner list: interchangeable phrasings of that one "
                           "concept - synonyms, variants, related terms "
                           "(ORed). 2-3 groups of 2-5 variants each.",
        },
        "query": {
            "type": "string",
            "description": "Flat keyword fallback for APIs without boolean "
                           "support: the 2-6 most important terms, quoting "
                           'multi-word phrases, e.g. "gaussian copula" tail '
                           "dependence. No boolean operators.",
        },
        "headline": {
            "type": "string",
            "description": "A 3-7 word label for this question, the way it "
                           "would head a section: no trailing punctuation, "
                           'just the subject. E.g. "Remote work and employee '
                           'wellbeing".',
        },
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "The key concepts in the question."},
        "areas": {"type": "array", "items": {"type": "string"},
                  "description": "Research areas the question belongs to "
                                 "(e.g. 'labor economics'), if clear."},
        "year_from": {"type": ["integer", "null"],
                      "description": "Only if the question names a period."},
        "year_to": {"type": ["integer", "null"],
                    "description": "Only if the question names a period."},
    },
    "required": ["concept_groups", "query", "headline"],
}

PARSE_QUESTION_INSTRUCTIONS = (
    "Turn the researcher's free-text research question into search queries "
    "for scholarly APIs (OpenAlex, Semantic Scholar, Crossref).\n\n"
    "concept_groups is the primary output: the boolean structure of the "
    "search. Each group is one concept the results must contain (groups are "
    "ANDed together); within a group, list interchangeable phrasings of that "
    "concept - synonyms, spelling variants, closely related terms (ORed "
    "together). Use only 2-3 groups: every extra group shrinks the result set "
    "sharply, and requiring more than three distinct concepts usually returns "
    "almost nothing. Be generous with variants inside a group (e.g. "
    "'narrative economics' alongside 'economic narratives'); recall matters "
    "more than precision because results are ranked for relevance "
    "afterwards. Quote nothing - phrases are quoted automatically.\n\n"
    "query is a flat fallback for APIs without boolean support: the 2-6 most "
    "important terms, multi-word phrases quoted.\n\n"
    "Also give a short headline, the extracted keywords and, when "
    "identifiable, the research areas. Only set year_from/year_to if the "
    "question explicitly mentions a time period."
)

# --------------------------------------------------------------- rank_papers

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer",
                           "description": "The paper's id, as given."},
                    "score": {"type": "number",
                              "description": "Relevance 0-100 to the question."},
                    "reason": {"type": "string",
                               "description": "One short sentence explaining "
                                              "the score."},
                },
                "required": ["id", "score", "reason"],
            },
        }
    },
    "required": ["rankings"],
}

RANK_INSTRUCTIONS = (
    "You are screening papers for a literature review. For each paper you get "
    "the id, title and abstract. Score how relevant each paper is to the "
    "researcher's question on a 0-100 scale (100 = directly on-topic, must "
    "read; 50 = related but tangential; 0 = irrelevant) and give a "
    "one-sentence reason. Score EVERY paper in the batch, using exactly the "
    "ids given. Judge from the title and abstract only; a paper without an "
    "abstract is judged on its title."
)

# ------------------------------------------------------------ annotate_paper

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "A 3-8 word label naming what "
                                             "the passage shows."},
                    "quote": {"type": "string",
                              "description": "VERBATIM excerpt copied "
                                             "character-for-character from "
                                             "the text given."},
                    "page": {"type": "integer",
                             "description": "The page number from the "
                                            "<page N> tag the quote is in."},
                    "why": {"type": "string",
                            "description": "One line on how the passage bears "
                                           "on the question."},
                    "about": {"type": "string",
                              "description": "Only when the passage reports "
                                             "another work's finding: that "
                                             "work as the paper cites it, e.g. "
                                             "'Park & Gupta (2012)'. Empty "
                                             "otherwise."},
                },
                "required": ["title", "quote", "page", "why"],
            },
        }
    },
    "required": ["findings"],
}

ANNOTATE_INSTRUCTIONS = (
    "You are helping with a literature review. You get the text of a paper "
    "(or one part of it), tagged with page numbers, and a research question. "
    "Find the passages relevant to the question.\n\n"
    "Rules:\n"
    "- title: a 3-8 word label naming what the passage shows (it heads the "
    "annotation card in the app).\n"
    "- quote: a VERBATIM excerpt copied character-for-character from the "
    "text you were given. This is critical: the app finds each quote in the "
    "PDF to highlight it, and a paraphrase cannot be found. Copy, don't "
    "retype; keep each quote to at most a few sentences; don't join "
    "sentences from different places with an ellipsis.\n"
    "- page: the page number of the <page N> tag the quote sits in.\n"
    "- why: one line explaining how the passage bears on the question, "
    "including whether it supports or contradicts it where applicable.\n"
    "- A passage may run over a line break or a page break: copy it as one "
    "quote, joining the lines with a space and writing a hyphenated "
    "line-end word whole ('treat-' + 'ment' -> 'treatment'; but keep the "
    "hyphen of a real compound like 'log-likelihood'). page is the <page N> "
    "tag where the quote starts.\n"
    "- The <page N> tags count the PDF's pages; a page number printed in a "
    "running header is not the tag - always give the tag.\n"
    "- From a table, quote the row with its label, so the passage says what "
    "it is about; or name the row and column in title.\n"
    "- The paper's own results come first. A passage that reports another "
    "work's finding is welcome when it answers the question - give that "
    "work in about, and say in why that this paper reports it.\n"
    "- Finding nothing relevant is a valid outcome: return an empty list "
    "rather than stretching for marginal passages.\n"
    "- When this task is one part of a longer paper, quote only from the "
    "pages given here; the other parts are separate tasks.\n"
    "- A software manual, a draft with tracked changes, or text garbled by "
    "the PDF is still read: quote only what reads cleanly, and say in why "
    "what kind of source it is when that matters."
)

# ---------------------------------------------------------------- synthesize

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "The answer text: plain paragraphs separated by "
                           "blank lines, with [CIT:key] markers.",
        },
    },
    "required": ["summary"],
}

SYNTH_INSTRUCTIONS = (
    "Write the synthesis section of a literature review. You get a research "
    "question and a list of evidence passages extracted from papers. Each "
    "passage has a citation key like [CIT:12-0] and shows the paper it comes "
    "from, a verbatim quote and a note on its relevance.\n\n"
    "Write a coherent, publication-ready answer to the research question "
    "based ONLY on the provided passages.\n"
    "Rules:\n"
    "- Place a [CIT:key] marker immediately after every claim it supports, "
    "exactly as given (the app renders these as APA citations).\n"
    "- Only use the provided keys; never invent citations, and do not write "
    "author names or years yourself - the marker becomes the citation. The "
    "one exception: a passage marked 'reports:' describes another work's "
    "finding, so attribute it to that work as reported - e.g. 'Park and "
    "Gupta's (2012) estimator, as reviewed in [CIT:key], ...'.\n"
    "- Cover agreements and disagreements between papers where visible.\n"
    "- Plain text paragraphs separated by blank lines. No headings, no "
    "markdown, no reference list (the app adds it).\n"
    "- If the passages cannot answer the question, say so briefly."
)

REVISE_INSTRUCTIONS = (
    "Edit the synthesis section of a literature review. You get the research "
    "question, the current draft (plain text with [CIT:key] citation "
    "markers), the available evidence passages, and an instruction from the "
    "author.\n\n"
    "Apply the instruction. Rules:\n"
    "- Keep [CIT:key] markers attached to the claims they support; add "
    "markers from the evidence list where they support new or changed "
    "claims. Never invent keys, never write author names or years yourself.\n"
    "- Do not change parts of the text the instruction does not touch, beyond "
    "what is needed for flow.\n"
    "- Plain text paragraphs separated by blank lines. No headings, no "
    "markdown, no reference list."
)

# ------------------------------------------------------------ assess_article

# Where a fit stops being worth annotating for. Below this for every question,
# Claude is asked what question the article WOULD answer.
FIT_THRESHOLD = 80.0

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "integer"},
                    "score": {"type": "number",
                              "description": "Relevance 0-100 of this article "
                                             "to this research question."},
                    "assessment": {"type": "string",
                                   "description": "One or two sentences: what "
                                                  "it would contribute to this "
                                                  "question, or why not."},
                },
                "required": ["question_id", "score", "assessment"],
            },
        },
        "misfit": {"type": "boolean",
                   "description": "true if the article does not belong in this "
                                  "project at all."},
        "suggested_question": {"type": "string",
                               "description": "Only when nothing fits but the "
                                              "article belongs: one research "
                                              "question it helps answer. "
                                              "Empty otherwise."},
        "suggestion_reason": {"type": "string",
                              "description": "Why that question is worth "
                                             "asking - or, for a misfit, what "
                                             "the article is about. Empty when "
                                             "an existing question fits."},
    },
    "required": ["fits", "misfit", "suggested_question", "suggestion_reason"],
}

FIT_INSTRUCTIONS = (
    "A researcher has found one article and wants to know which of their "
    "research questions it belongs to. You get the project, its research "
    "questions with their ids, and the article. Score the article 0-100 "
    "against EVERY question (100 = directly answers it, must read; 50 = "
    "related but tangential; 0 = irrelevant) and say in one or two sentences "
    "what it would contribute there, or why it would not. Judge the article "
    "as evidence for that particular question, not by its general quality.\n\n"
    f"If no question scores {FIT_THRESHOLD:.0f} or more, decide which case "
    "you are in. If the article fits the project's direction but none of the "
    "questions asked so far, propose ONE research question the researcher "
    "could add that this article would help answer - specific enough to run "
    "a literature search on, and in the spirit of the questions already "
    "there. If instead the article does not belong in this project at all, "
    "set misfit true, leave suggested_question empty, and say plainly in "
    "suggestion_reason what the article is about and why it does not fit. "
    "Never invent a question just to have one."
)

# ----------------------------------------------------------------- parse_pdf

PARSE_PDF_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer",
                             "description": "The PDF page number (1-based)."},
                    "markdown": {"type": "string",
                                 "description": "That page's content as "
                                                "markdown."},
                },
                "required": ["page", "markdown"],
            },
        }
    },
    "required": ["pages"],
}

PARSE_PDF_INSTRUCTIONS = (
    "Transcribe the PDF pages listed in this task into markdown, one entry "
    "per page, so the app can search, quote and annotate the document. "
    "Faithfulness matters more than polish: researchers will quote this text "
    "and the app matches quotes back to the PDF.\n\n"
    "Rules:\n"
    "- Keep the words exactly as printed, in reading order (follow columns "
    "down, left column first). Don't summarize, translate or correct.\n"
    "- Headings as markdown headings (#, ##), lists as lists.\n"
    "- Tables as markdown tables, keeping every row and column.\n"
    "- Equations as LaTeX: $...$ inline, $$...$$ for display equations.\n"
    "- Figures: one line in square brackets with the caption, e.g. "
    "[Figure 2: Employment by sector, 1990-2020].\n"
    "- Where the PDF's maths font shows the wrong letters (s for sigma, "
    "a stray 'ò' for an integral), write the symbol the author meant, in "
    "LaTeX.\n"
    "- A draft with tracked changes: transcribe the text as it reads after "
    "the changes, and leave out comment balloons and 'Formatted:' notes.\n"
    "- Leave out running headers, footers and bare page numbers.\n"
    "- A blank or image-only page gets an empty markdown string.\n"
    "- Submit every page listed in the task, even when a task covers many: "
    "pages you leave out stay unread."
)


# ------------------------------------------------------------- verify_answer

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence": {"type": "integer",
                                 "description": "The number of the sentence, "
                                                "as given."},
                    "key": {"type": "string",
                            "description": "The citation key, as given."},
                    "verdict": {"type": "string",
                                "enum": ["supported", "partial", "unsupported"]},
                    "note": {"type": "string",
                             "description": "One short sentence: what the "
                                            "passage does and does not show. "
                                            "Empty for a plain 'supported'."},
                },
                "required": ["sentence", "key", "verdict", "note"],
            },
        },
        "overall": {"type": "string",
                    "description": "Two or three sentences for the author: how "
                                   "well the answer is supported, and what to "
                                   "fix first."},
    },
    "required": ["checks", "overall"],
}

VERIFY_INSTRUCTIONS = (
    "Check a literature-review answer against its evidence, citation by "
    "citation. You get the answer split into numbered sentences - with the "
    "[CIT:key] markers where the author put them, so you can see which clause "
    "each one backs - and for every key the verbatim passage it points to, "
    "with the paper, page, section and a little surrounding context. "
    "located says where the passage was found: pdf (highlighted in the "
    "PDF), text (in the paper's text, not highlighted), none (not found - "
    "be wary). Sentences that carry no citation are listed apart; mention in "
    "overall any that make a claim needing one.\n\n"
    "For every (sentence, key) pair, judge whether the passage supports what "
    "the sentence claims:\n"
    "- supported: the passage says it.\n"
    "- partial: the passage supports part of the claim, a weaker version of "
    "it, or only with a qualification the sentence leaves out - or the "
    "sentence attributes to this paper something that is another paper's "
    "contribution.\n"
    "- unsupported: the passage does not say it, or says something "
    "different.\n"
    "Judge only from the passage and its context, never from what you know "
    "about the paper otherwise. Be strict: a citation that only sounds "
    "related is not support. Give a note for every partial or unsupported "
    "verdict. Check every pair given - do not skip any."
)
