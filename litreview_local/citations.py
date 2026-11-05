"""Citations: APA rendering, summary token <-> HTML conversion, sanitizing.

The synthesized answer is stored as HTML. In-text citations are spans like

    <cite class="cit" data-k="12-9f8a1c3d">(Smith &amp; Jones, 2020)</cite>

where data-k is "{annotation_id}-{quote digest}" - one specific highlighted
passage of one paper. The digest is content-addressed (a hash of the quote),
so a key keeps pointing at the same passage when a paper is re-annotated and
stops resolving once that passage is gone. It can never silently re-point at
a different passage, which a positional index did: re-annotation rewrites a
paper's findings wholesale, so "the 4th finding" is a different quote after
every run.

The LLM never sees HTML: prompts use plain text with [CIT:12-9f8a1c3d]
tokens, converted here in both directions.
"""
import hashlib
import html as html_mod
import re
from html.parser import HTMLParser

# {annotation_id}-{hex digest}. Also matches the legacy positional form
# ({annotation_id}-{index}), so summaries written before content-addressing
# still parse - those keys simply no longer resolve and render as
# unavailable, which is the point.
KEY_PATTERN = r"[0-9]+-[0-9a-f]+"
TOKEN_RE = re.compile(r"\[CIT:(" + KEY_PATTERN + r")\]")
_CITE_KEY_RE = re.compile(r'data-k="(' + KEY_PATTERN + r')"')
# A whole stored citation, marked or not. Rendering goes through the element
# rather than its opening tag because the marker lives in both: the class
# says it is broken and the text says "(citation unavailable)", and a
# citation that resolves again has to lose both.
_CITE_EL_RE = re.compile(
    r'<cite class="cit(?: cit-missing)?" data-k="(' + KEY_PATTERN + r')"\s*>.*?</cite>',
    re.S)
DIGEST_CHARS = 8


# ------------------------------------------------------------------ APA text

EN_DASH = "\N{EN DASH}"
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

# Lowercase words that belong to the surname after them: van Gogh, de Jong,
# von Hippel, van der Berg, van 't Hoff, do Carmo.
_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "dello",
              "degli", "dei", "di", "da", "dal", "dalla", "das", "dos", "do",
              "du", "des", "la", "le", "ter", "ten", "te", "zu", "zum", "zur",
              "vom", "'t", "\N{RIGHT SINGLE QUOTATION MARK}t"}
# The ones that are particles capitalized too - Van den Bulte, De Loecker,
# Di Tella, La Porta, Van Reenen - when the name does not begin with them.
_CAPITAL_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di",
                      "da", "dal", "du", "des", "dos", "das", "la", "le",
                      "ter", "ten"}
# Before these family names a capitalized "Van" (or "Le") is a Vietnamese
# middle name - Thanh Van Nguyen is Nguyen, T. V. - not a particle.
_VIETNAMESE_FAMILIES = {"nguyen", "tran", "le", "pham", "hoang", "huynh",
                        "phan", "vu", "vo", "dang", "bui", "do", "ho", "ngo",
                        "duong", "ly", "trinh", "dinh", "doan", "lam", "luong",
                        "mai", "truong", "cao", "ha"}
# A name with one of these words is an organization's - an uploaded report's
# issuer, usually - and is cited whole: World Health Organization, not
# Organization, W. H.
_ORGANIZATION_WORDS = {
    "organization", "organisation", "association", "institute", "institution",
    "university", "college", "school", "bank", "agency", "commission",
    "council", "committee", "department", "ministry", "office", "bureau",
    "board", "authority", "service", "services", "society", "foundation",
    "fund", "trust", "group", "consortium", "network", "alliance",
    "federation", "union", "centre", "center", "corporation", "company",
    "inc", "ltd", "llc", "plc", "gmbh", "team", "project", "initiative",
    "partnership", "coalition", "collaboration", "collaborators",
    "investigators", "programme", "program", "forum", "panel", "secretariat",
    "parliament", "government", "administration", "statistics",
    "observatory", "laboratory", "academy", "nations"}
_CONNECTIVES = {"of", "for", "and", "the", "&"}
_HYPHENS = "-\N{HYPHEN}\N{NON-BREAKING HYPHEN}\N{FIGURE DASH}\N{EN DASH}"
# One given name ("Jan", "D'Arcy") or a hyphen between two.
_NAME_UNIT = re.compile(r"[^\W\d_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W\d_]+)*"
                        r"|[" + _HYPHENS + r"]")


def is_organization(name: str) -> bool:
    """Whether an author is a body rather than a person (by its wording)."""
    words = [w.strip(".,()").lower() for w in (name or "").split()]
    return (len(words) > 1
            and (any(w in _ORGANIZATION_WORDS or w in _CONNECTIVES for w in words)
                 or any(ch.isdigit() for ch in name)))


def _suffix(word: str) -> str:
    """"Jr." for jr, Jr or JR.; "III" for III - or "" for any other word.
    Numerals only in capitals: Ii is a Japanese family name."""
    bare = word.rstrip(".").lower()
    if bare in ("jr", "jnr"):
        return "Jr."
    if bare in ("sr", "snr"):
        return "Sr."
    return word if word in ("II", "III", "IV") else ""


def split_name(name: str) -> tuple[str, str, str]:
    """(surname, given names, suffix) of one author, written the way the
    sources write them: "Jan-Michael Becker", "Becker, Jan-Michael",
    "Ludwig van Beethoven", "Christophe Van den Bulte", "Martin Luther King
    Jr.". An organization, or a name of one word, is all surname:
    ("World Health Organization", "", "")."""
    name = " ".join((name or "").split())
    if not name:
        return "", "", ""
    if is_organization(name):
        return name, "", ""
    if "," in name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        rest = parts[1:]
        suffix = " ".join(_suffix(p) for p in rest if _suffix(p))
        given = " ".join(p for p in rest if not _suffix(p))
        return parts[0], given, suffix
    tokens = name.split(" ")
    suffix = ""
    if len(tokens) > 1 and _suffix(tokens[-1]):
        suffix = _suffix(tokens.pop())
    if len(tokens) == 1:
        return tokens[0], "", suffix
    core = tokens[-1].lower()
    start = len(tokens) - 1
    while start > 0:
        word = tokens[start - 1]
        if word.islower() and word in _PARTICLES:
            start -= 1  # a lowercase particle, even one opening the name
        elif (start - 1 > 0 and word[:1].isupper()
              and word.lower() in _CAPITAL_PARTICLES
              and core not in _VIETNAMESE_FAMILIES):
            start -= 1  # capitalized, after a given name
        else:
            break
    return " ".join(tokens[start:]), " ".join(tokens[:start]), suffix


def initials(given: str) -> str:
    """APA's initials of given names: "Jan-Michael" -> "J.-M.", "Christian
    M." -> "C. M.", "J.M." -> "J. M."."""
    out: list[str] = []
    joined = False
    for unit in _NAME_UNIT.findall(given or ""):
        if unit in _HYPHENS:
            joined = bool(out)
            continue
        if unit.islower() and unit in _PARTICLES:  # María del Carmen
            joined = False
            continue
        letter = unit[0].upper() + "."
        if joined:
            out[-1] += "-" + letter
        else:
            out.append(letter)
        joined = False
    return " ".join(out)


def _surname(name: str) -> str:
    """The family name from 'First Last' or 'Last, First', particles kept:
    'van der Berg'."""
    return split_name(name)[0] or "Anon."


def apa_inline(authors: list, year) -> str:
    """(Smith, 2020) / (Smith & Jones, 2020) / (Smith et al., 2020)."""
    year_s = str(year) if year else "n.d."
    surnames = [_surname(a) for a in (authors or []) if (a or "").strip()]
    if not surnames:
        return f"(Anon., {year_s})"
    if len(surnames) == 1:
        return f"({surnames[0]}, {year_s})"
    if len(surnames) == 2:
        return f"({surnames[0]} & {surnames[1]}, {year_s})"
    return f"({surnames[0]} et al., {year_s})"


def apa_author(name: str) -> str:
    """One author as an APA reference names them: "Becker, J.-M.",
    "van Heerde, H. J.", "King, M. L., Jr.", "World Health Organization"."""
    surname, given, suffix = split_name(name)
    return ", ".join(part for part in (surname, initials(given), suffix) if part)


def _apa_authors(authors) -> str:
    """The author element: every author up to 20, "&" before the last; from
    21 on the first 19, an ellipsis and the last. Ends in a full stop."""
    names = [n for n in (apa_author(a) for a in authors or [] if isinstance(a, str)) if n]
    if not names:
        return ""
    if len(names) == 1:
        who = names[0]
    elif len(names) <= 20:
        who = ", ".join(names[:-1]) + ", & " + names[-1]
    else:
        who = ", ".join(names[:19]) + f", {ELLIPSIS} " + names[-1]
    return who if who.endswith(".") else who + "."


def _is_article_number(pages: str) -> bool:
    """104360 or e0210000 - one number standing for the article - as against
    a page (46) or a range (46-66)."""
    return bool(re.fullmatch(r"\d{5,}|e[0-9a-z]*\d[0-9a-z]*", pages, re.I))


# How each kind of work is cited. Journal articles are the default; these
# stand alone (the title in italics), or sit in a book or proceedings.
_STANDALONE_TYPES = {"book", "monograph", "report", "working-paper", "preprint",
                     "dissertation", "thesis", "policy-brief", "guideline",
                     "presentation", "web-page", "dataset", "standard"}
_CONTAINED_TYPES = {"book-chapter", "conference-paper"}


def _apa_runs(paper) -> list[tuple[str, bool]]:
    """The reference as (text, italic) runs, for both renderings."""
    def text(field: str) -> str:
        return " ".join(str(getattr(paper, field, None) or "").split())

    year = getattr(paper, "year", None) or getattr(paper, "online_year", None)
    title, venue = text("title") or "Untitled", text("venue")
    volume, issue, pages = text("volume"), text("issue"), text("pages")
    kind = getattr(paper, "work_type", None)
    standalone = kind in _STANDALONE_TYPES

    what: list[tuple[str, bool]] = [(title, standalone)]
    if not title.endswith((".", "?", "!")):
        what.append((".", False))
    source: list[tuple[str, bool]] = []
    if venue and kind in _CONTAINED_TYPES:
        source = [("In ", False), (venue, True)]
        if pages:
            label = "pp." if "-" in pages else "p."
            source.append((f" ({label} {pages.replace('-', EN_DASH)})", False))
        source.append((".", False))
    elif venue and standalone:
        source = [(venue if venue.endswith(".") else venue + ".", False)]
    elif venue:
        source = [(venue, True)]
        if volume:
            source += [(", ", False), (volume, True)]
            if issue:
                source.append((f"({issue.replace('-', EN_DASH)})", False))
        if pages:
            source.append((", " + (f"Article {pages}" if _is_article_number(pages)
                                   else pages.replace("-", EN_DASH)), False))
        source.append((".", False))
    doi = text("doi")
    link = f"https://doi.org/{doi}" if doi else text("url")

    who = _apa_authors(getattr(paper, "authors", None) or [])
    when = [(f"({year})." if year else "(n.d.).", False)]
    # Without an author, the title takes the author's place, as APA has it.
    elements = [[(who, False)], when, what] if who else [what, when]
    elements += [source, [(link, False)] if link else []]
    runs: list[tuple[str, bool]] = []
    for element in (e for e in elements if e):
        if runs:
            runs.append((" ", False))
        runs.extend(element)
    return runs


def apa_reference(paper) -> str:
    """The APA 7 reference, italics marked *like this*:

    Becker, J.-M., Proksch, D., & Ringle, C. M. (2022). Revisiting Gaussian
    copulas to handle endogenous regressors. *Journal of the Academy of
    Marketing Science*, *50*(1), 46–66. https://doi.org/10.1007/s11747-021-00805-y

    The year is the version of record's (paper.year); years_note says when
    the paper went online in another one.
    """
    return "".join(f"*{t}*" if italic else t for t, italic in _apa_runs(paper))


def apa_reference_html(paper) -> str:
    """apa_reference as HTML: escaped, with <i> for the italics."""
    return "".join(f"<i>{html_mod.escape(t)}</i>" if italic else html_mod.escape(t)
                   for t, italic in _apa_runs(paper))


def years_note(paper) -> str | None:
    """"online 2021" when a paper went online in another year than its issue
    came out - the reference gives the issue's year, which is what the field
    cites, and the UI can show this beside it. None when there is nothing to
    say."""
    year = getattr(paper, "year", None)
    online = getattr(paper, "online_year", None)
    if year and online and online != year:
        return f"online {online}"
    return None


# ------------------------------------------------------- citation lookup map

def quote_digest(quote: str) -> str:
    """Stable short hash of a passage, insensitive to whitespace differences."""
    normalized = re.sub(r"\s+", " ", quote or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def finding_key(annotation_id: int, finding: dict) -> str:
    """Content-addressed key for one finding of one annotation."""
    return f"{annotation_id}-{quote_digest(finding.get('quote', ''))}"


def _shown(quote: str) -> str:
    from .highlight import _display  # highlight builds on this module's users

    return _display(quote or "", quote or "") if "\n" in (quote or "") else (quote or "")


def citation_map(annotations) -> dict[str, dict]:
    """key -> everything the UI / prompts need about one finding.

    Unmatched findings are included: the quote could not be located in the
    PDF, but it is still a passage the model extracted, so it stays citable -
    with `matched` carried through so every consumer can say so.
    """
    cmap: dict[str, dict] = {}
    for a in annotations:
        paper = a.paper
        for f in (a.findings or []):
            if f.get("rejected"):
                # Failed the strict check when re-verified: not evidence.
                continue
            key = finding_key(a.id, f)
            if key in cmap:
                continue  # the same passage quoted twice - one entry is enough
            cmap[key] = {
                "key": key,
                "annotation_id": a.id,
                "paper_id": paper.id if paper else None,
                "title": f.get("title") or (paper.title if paper else ""),
                # As it reads, for every consumer: an older build stored some
                # quotes with the text layer's line breaks and line-end
                # hyphens ("inter-\ncept"). The stored quote - and so the
                # key - stays as it is.
                "quote": _shown(f.get("quote", "")),
                "why": f.get("why", ""),
                "page": f.get("page", 1),
                # The number printed on that page, when the paper's pages
                # are numbered differently from the PDF's (p. 78 of a
                # journal issue is the PDF's page 8). What a citation wants.
                "printed_page": f.get("printed_page"),
                "matched": bool(f.get("matched")),
                # pdf / text / none - see highlight.enrich_from_text. Older
                # rows carry only matched.
                "located": f.get("located") or ("pdf" if f.get("matched") else "none"),
                "section": f.get("section"),
                "context": f.get("context"),
                "added_by": f.get("added_by"),
                # The work a passage reports on, when it is not this paper's
                # own finding (a review's summary of someone else).
                "about": f.get("about") or None,
                # Another key of the very same passage (added twice): it
                # still resolves, but is listed once.
                "alias_of": f.get("duplicate_of") or None,
                "apa": apa_inline(paper.authors if paper else [],
                                  paper.year if paper else None),
                "paper_title": paper.title if paper else "",
            }
    return cmap


def annotation_items(annotation, cmap: dict[str, dict]) -> list[dict]:
    """{key, finding, info} per finding of one annotation, in stored order."""
    items: list[dict] = []
    seen: set[str] = set()
    for f in (annotation.findings or []):
        key = finding_key(annotation.id, f)
        if key in seen:
            continue
        seen.add(key)
        items.append({"key": key, "finding": f, "info": cmap.get(key)})
    return items


# --------------------------------------------------- tokens <-> summary HTML

def resolve_key(key: str, cmap: dict[str, dict]) -> str | None:
    """The passage a citation key means, allowing for a truncated digest.

    The synthesis model transcribes these keys by hand, and now and then it
    drops the last character of one: an answer in this project cites
    "10-8c1308f" for a passage stored as "10-8c1308f4". The passage is right
    there and the citation renders as unavailable, which is a worse lie than
    resolving it. A prefix is accepted only when exactly one passage matches
    it, and only within the annotation the key already names, so a repair
    cannot silently point a quote at somebody else's paper.
    """
    if key in cmap:
        return key
    prefix, _, digest = key.partition("-")
    if not digest or len(digest) < 4:
        return None
    matches = [k for k in cmap if k.startswith(key)]
    return matches[0] if len(matches) == 1 else None


def cite_span(key: str, cmap: dict[str, dict]) -> str:
    resolved = resolve_key(key, cmap)
    info = cmap.get(resolved) if resolved else None
    if info is None:
        return (f'<cite class="cit cit-missing" data-k="{html_mod.escape(key)}">'
                "(citation unavailable)</cite>")
    # Stored under the key that resolved, so a repaired citation stays
    # repaired instead of needing the same guess on every render.
    return (f'<cite class="cit" data-k="{html_mod.escape(resolved)}">'
            f'{html_mod.escape(info.get("apa") or "(cited)")}</cite>')


_TRAILING_CITES = re.compile(
    r"([.!?;:])((?:\s*\[CIT:" + KEY_PATTERN + r"\])+)")


def place_citations(text: str) -> str:
    """Put citation markers where a manuscript wants them.

    Models tend to write "...the variable. [CIT:a] [CIT:b]", which pastes
    into Word as "variable.(A, 2021)(B, 2022)". A run of markers that follows
    sentence-final punctuation is moved in front of it, given a space, and
    rid of repeats: "...the variable [CIT:a] [CIT:b]." The merge into one
    parenthesis, "(A, 2021; B, 2022)", happens where the text is rendered for
    copying.
    """
    def move(match: re.Match) -> str:
        keys = TOKEN_RE.findall(match.group(2))
        unique = list(dict.fromkeys(keys))
        return " " + " ".join(f"[CIT:{k}]" for k in unique) + match.group(1)

    text = _TRAILING_CITES.sub(move, text or "")
    # Repeats of one key side by side, wherever they are.
    text = _REPEATED_CITE.sub(r"\1", text)
    # No space before punctuation, a space before every marker, no doubles.
    text = re.sub(r"[ \t]+([.!?;:,])", r"\1", text)
    text = re.sub(r"(\S)\[CIT:", r"\1 [CIT:", text)
    return re.sub(r"[ \t]{2,}", " ", text)


_REPEATED_CITE = re.compile(
    r"(\[CIT:(" + KEY_PATTERN + r")\])(?:\s*\[CIT:\2\])+")


def tokens_to_html(text: str, cmap: dict[str, dict]) -> str:
    """LLM output (paragraphs + [CIT:k] tokens) -> stored summary HTML."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    out = []
    for para in paragraphs:
        escaped = html_mod.escape(para.replace("\n", " "))
        rendered = TOKEN_RE.sub(
            lambda m: cite_span(m.group(1), cmap),
            # escaping does not touch [CIT:...] so substituting after is safe
            escaped,
        )
        out.append(f"<p>{rendered}</p>")
    return "\n".join(out)


def flag_missing_citations(html: str, cmap: dict[str, dict]) -> str:
    """Render every stored citation against the evidence as it stands now.

    Re-annotating a paper replaces its findings, and removing an article
    takes them away entirely, so a citation written earlier can name a
    passage that is gone. It must read as unavailable - never quietly
    resolve to whatever else now sits in that paper.

    The judgement is made here, at render time, rather than trusted from
    storage: a citation stored as unavailable can become resolvable again -
    a key whose digest was truncated, once resolve_key learns to repair it -
    and it has to shed both halves of the marker, the class and the
    "(citation unavailable)" text. Nothing is written back; the stored HTML
    catches up on the next save or rewrite.
    """
    if not html:
        return html or ""
    return _CITE_EL_RE.sub(lambda m: cite_span(m.group(1), cmap), html)


class _TokenExtractor(HTMLParser):
    """HTML -> plain text with [CIT:k] tokens (for prompts / re-synthesis)."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._cite_key: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "cite":
            self._cite_key = dict(attrs).get("data-k")
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "div"):
            self.parts.append("\n\n" if tag == "p" else "\n")

    def handle_endtag(self, tag):
        if tag == "cite":
            if self._cite_key:
                self.parts.append(f"[CIT:{self._cite_key}]")
            self._cite_key = None

    def handle_data(self, data):
        if self._cite_key is None:  # citation label text is regenerated later
            self.parts.append(data)


def html_to_tokens(html: str) -> str:
    parser = _TokenExtractor()
    parser.feed(html or "")
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cited_keys(html: str) -> list[str]:
    """Unique citation keys in the order they appear in the summary."""
    seen: list[str] = []
    for m in _CITE_KEY_RE.finditer(html or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


# ------------------------------------------------------------- sanitization

_ALLOWED_TAGS = {"p", "br", "strong", "em", "b", "i", "u", "ul", "ol", "li",
                 "h1", "h2", "h3", "h4", "blockquote", "cite", "span", "div"}
_VOID_TAGS = {"br"}


class _Sanitizer(HTMLParser):
    """Allowlist sanitizer for the contenteditable summary.

    Keeps structural tags without attributes; <cite> keeps only a valid
    data-k. Everything else (scripts, styles, event handlers, ...) is dropped.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0  # inside a dropped element like <script>

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            self._skip_depth += 1
            return
        if tag in ("script", "style"):
            self._skip_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "cite":
            key = dict(attrs).get("data-k", "")
            if re.fullmatch(KEY_PATTERN, key or ""):
                # the cit-missing marker is recomputed at render time
                self.out.append(f'<cite class="cit" data-k="{key}">')
            else:
                self.out.append("<cite>")
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip_depth:
            self.out.append(html_mod.escape(data))


def sanitize_summary_html(html: str) -> str:
    parser = _Sanitizer()
    parser.feed(html or "")
    return "".join(parser.out).strip()


# ---------------------------------------------------------------- reference list

def reference_list(html: str, cmap: dict[str, dict], papers_by_id: dict) -> list:
    """Unique cited papers, ordered by first appearance in the summary."""
    refs = []
    seen_papers: set[int] = set()
    for key in cited_keys(html):
        info = cmap.get(resolve_key(key, cmap) or key)
        if not info or info["paper_id"] in seen_papers:
            continue
        paper = papers_by_id.get(info["paper_id"])
        if paper is None:
            continue
        seen_papers.add(paper.id)
        refs.append(paper)
    return refs
