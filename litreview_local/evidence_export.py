"""An answer as a manuscript, with the evidence behind every citation.

Once text leaves the app, nobody can hover a citation any more - so this
export carries the evidence with it: the answer in APA style (citations
merged and before the full stop), the reference list, and an evidence
appendix giving, for every citation, the claim, the verbatim passage, its
page, whether the passage was found in the PDF, Claude's check of it when
there is one, and a link that opens the highlighted PDF at that page while
LitReview is running.

Two forms: Markdown (for a manuscript tool or a repository) and a standalone
HTML page (for reading, and for pasting into Word with the links intact).
"""
import html
import re

from sqlalchemy.orm import Session

from . import citations, models, research

LOCATED = {"pdf": "in the PDF", "text": "in the text only",
           "none": "not found"}


def _merge_run(keys: list[str], cmap: dict) -> str:
    """One parenthesis for a run of markers: (A, 2021; B, 2022)."""
    labels = []
    for key in dict.fromkeys(keys):
        resolved = citations.resolve_key(key, cmap)
        info = cmap.get(resolved) if resolved else None
        label = (info["apa"] if info else "(citation unavailable)").strip("()")
        if label not in labels:
            labels.append(label)
    return "(" + "; ".join(labels) + ")"


_RUN = re.compile(r"\[CIT:[^\]]+\](?:\s*\[CIT:[^\]]+\])*")


def render(tokens: str, cmap: dict, *, numbered: dict | None = None) -> str:
    """Answer tokens to text with merged APA citations; with `numbered`
    ({key: n}), each citation is followed by its evidence number in
    brackets."""
    def replace(match: re.Match) -> str:
        keys = citations.TOKEN_RE.findall(match.group(0))
        text = _merge_run(keys, cmap)
        if numbered:
            refs = ",".join(str(numbered[k]) for k in dict.fromkeys(keys) if k in numbered)
            if refs:
                text += f"[{refs}]"
        return text

    return _RUN.sub(replace, citations.place_citations(tokens))


def build(db: Session, rq: models.ResearchQuestion, base: str) -> dict:
    """{"title", "answer", "references", "evidence": [...]} for one question."""
    annotations = research.question_annotations(db, rq.id)
    cmap = citations.citation_map(annotations)
    tokens = citations.html_to_tokens(rq.summary_html or "")
    check = research.verification_for(rq) or {}
    verdicts = check.get("verdicts") or {}
    evidence = []
    numbered: dict[str, int] = {}
    for sentence in research.answer_sentences(rq):
        for key in sentence["keys"]:
            if key in numbered:
                continue
            resolved = citations.resolve_key(key, cmap)
            info = cmap.get(resolved) if resolved else None
            numbered[key] = len(evidence) + 1
            entry = {"n": numbered[key], "key": key, "claim": sentence["text"]}
            if info is None:
                entry.update(missing=True)
            else:
                entry.update(
                    citation=info["apa"], paper=info["paper_title"],
                    quote=info["quote"], page=info["page"],
                    located=info.get("located") or "none",
                    section=info.get("section"), context=info.get("context"),
                    link=f"{base}/annotations/{info['annotation_id']}/view"
                         f"?page={info['page']}")
                verdict = verdicts.get(key)
                if verdict:
                    entry.update(verdict=verdict["verdict"],
                                 verdict_note=verdict.get("note") or "")
            evidence.append(entry)
    papers = {a.paper.id: a.paper for a in annotations if a.paper}
    references = [citations.apa_reference(p) for p in
                  citations.reference_list(rq.summary_html or "", cmap, papers)]
    return {"title": rq.headline or rq.text, "question": rq.text,
            "label": rq.label,
            "answer": render(tokens, cmap),
            "answer_numbered": render(tokens, cmap, numbered=numbered),
            "references": references, "evidence": evidence,
            "checked": check.get("checked_at"), "overall": check.get("overall")}


def to_markdown(doc: dict) -> str:
    lines = [f"# {doc['title']}", "", f"*{doc['label']}: {doc['question']}*", "",
             doc["answer_numbered"], "", "## References", ""]
    lines += [f"- {ref}" for ref in doc["references"]] or ["(none)"]
    lines += ["", "## Evidence", "",
              "Each number in brackets above points to the passage below. "
              "Links open the highlighted PDF while LitReview is running.", ""]
    if doc.get("overall"):
        lines += [f"**Claude's check:** {doc['overall']}", ""]
    for entry in doc["evidence"]:
        if entry.get("missing"):
            lines.append(f"{entry['n']}. *{entry['claim']}* - the passage behind "
                         "this citation is no longer in the review.")
            continue
        where = f"p. {entry['page']}" + (f", {entry['section']}" if entry.get("section") else "")
        found = LOCATED.get(entry["located"], entry["located"])
        lines.append(f"{entry['n']}. **{entry['citation']}** {entry['paper']} ({where}; "
                     f"{found}) - [open]({entry['link']})")
        lines.append(f"   - Claim: {entry['claim']}")
        lines.append(f"   - Passage: \"{entry['quote']}\"")
        if entry.get("verdict"):
            note = f" - {entry['verdict_note']}" if entry.get("verdict_note") else ""
            lines.append(f"   - Check: {entry['verdict']}{note}")
    return "\n".join(lines) + "\n"


def to_html(doc: dict) -> str:
    esc = html.escape
    rows = []
    for entry in doc["evidence"]:
        if entry.get("missing"):
            rows.append(f"<li id='e{entry['n']}'><em>{esc(entry['claim'])}</em> - the "
                        "passage behind this citation is no longer in the review.</li>")
            continue
        where = f"p. {entry['page']}" + (f", {esc(entry['section'])}" if entry.get("section") else "")
        verdict = ""
        if entry.get("verdict"):
            verdict = (f"<div class='v v-{esc(entry['verdict'])}'>Check: "
                       f"{esc(entry['verdict'])}"
                       + (f" - {esc(entry['verdict_note'])}" if entry.get("verdict_note") else "")
                       + "</div>")
        rows.append(
            f"<li id='e{entry['n']}'><strong>{esc(entry['citation'])}</strong> "
            f"{esc(entry['paper'])} <span class='m'>({where}; "
            f"{esc(LOCATED.get(entry['located'], entry['located']))})</span> "
            f"<a href='{esc(entry['link'])}'>open</a>"
            f"<div class='c'>{esc(entry['claim'])}</div>"
            f"<blockquote>{esc(entry['quote'])}</blockquote>{verdict}</li>")
    answer = esc(doc["answer_numbered"])
    answer = re.sub(r"\[(\d+(?:,\d+)*)\]",
                    lambda m: "<sup>" + ",".join(
                        f"<a href='#e{n}'>{n}</a>" for n in m.group(1).split(",")) + "</sup>",
                    answer)
    paragraphs = "".join(f"<p>{p}</p>" for p in answer.split("\n\n") if p.strip())
    refs = "".join(f"<li>{esc(r)}</li>" for r in doc["references"])
    overall = (f"<p class='o'><strong>Claude's check:</strong> {esc(doc['overall'])}</p>"
               if doc.get("overall") else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{esc(doc['title'])}</title>
<style>
body{{font:16px/1.55 Georgia,serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#222}}
h1{{font-size:1.6rem}} .q{{color:#555;font-style:italic}}
sup a{{text-decoration:none}} li{{margin:.9rem 0}} .m{{color:#666;font-size:.9em}}
.c{{margin:.2rem 0;color:#333}} blockquote{{margin:.3rem 0 .3rem 1rem;padding-left:.7rem;border-left:3px solid #c9a227;color:#333}}
.v{{font-size:.9em}} .v-supported{{color:#2e7d32}} .v-partial{{color:#a06400}} .v-unsupported{{color:#b00020}}
.o{{background:#f6f3ea;padding:.6rem .8rem}}
</style></head><body>
<h1>{esc(doc['title'])}</h1>
<p class="q">{esc(doc['label'])}: {esc(doc['question'])}</p>
{paragraphs}
<h2>References</h2><ul>{refs}</ul>
<h2>Evidence</h2>
<p class="m">Each number in the text points to the passage below. Links open the
highlighted PDF while LitReview is running on this computer.</p>
{overall}<ol>{''.join(rows)}</ol>
</body></html>
"""
