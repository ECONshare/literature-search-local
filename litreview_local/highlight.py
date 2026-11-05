"""Find Claude's quotes in the PDF and highlight them.

Claude reads a paper's text and answers with verbatim quotes and page
numbers (an annotate_paper task). Every quote kept is the source's own words
(match_quote), and here each is located in the PDF and highlighted, and the
result is written as that annotation's own copy of the PDF (the rationale is
not put in the highlight: a viewer would draw a note icon over the passage
for it):

1. The passage is found in the paper's stored text, which says exactly which
   PDF page it starts on.
2. That page (then its neighbours) is read again, character by character,
   with the same PyMuPDF flags and the same cleaning the stored text was made
   with (text_flags, clean_text), and the passage is found there under the
   same normalization match_quote uses. Ligatures, line-end hyphens, curly
   quotes, non-breaking spaces, soft hyphens and the like therefore look the
   same on both sides, and a passage that runs onto the next page is found
   across the break.
3. Failing that, sentence by sentence, which survives a passage whose middle
   crosses a column break or a stored text Claude transcribed differently.
4. A quote that still cannot be placed is kept and reported (matched=False),
   never dropped - it is still evidence, only without a highlight.

The geometry of every highlight is stored with the finding, so the
highlighted copy is a derived file: delete it and the next view redraws it
from the untouched original.
"""
import array
import functools
import logging
import re
import unicodedata
import uuid
from pathlib import Path

from . import pdfs

logger = logging.getLogger(__name__)


# ======================================================== the text layer

@functools.cache
def text_flags() -> int:
    """The PyMuPDF flags every read of a PDF's text layer uses - extraction
    (paper_texts) and the highlighter alike - so that what one finds, the
    other finds too:

    - ligatures expanded (no TEXT_PRESERVE_LIGATURES): "ﬁ" reads "fi";
    - every kind of space read as a plain space (no TEXT_PRESERVE_WHITESPACE);
    - no dehyphenation (no TEXT_DEHYPHENATE): MuPDF cannot tell "treat-|ment"
      from "self-|employed" and joins both, so a line-end hyphen stays in the
      text; matching treats it as optional (_norm_map) and a stored quote
      keeps or joins it the way the quote has it (_display);
    - nothing outside the page's mediabox (TEXT_MEDIABOX_CLIP);
    - a glyph with no Unicode mapping read as its character code
      (TEXT_CID_FOR_UNKNOWN_UNICODE, as in MuPDF's own defaults - it is what
      makes symbolic fonts readable at all); clean_text drops the control
      characters that come out of it.
    """
    import pymupdf

    return pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE


# Spaces of every width - non-breaking, Ogham, en and em quads and spaces,
# three/four/six-per-em, figure, punctuation, thin, hair, narrow
# non-breaking, medium mathematical, ideographic - are one plain space.
_WIDE_SPACES = frozenset("\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
                         "\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000")
# Characters that are never seen, only break words up for search and copy:
# zero-width space, non-joiner and joiner, the word joiner, the invisible
# maths operators, the byte-order mark, the Mongolian vowel separator.
# (Soft hyphens: see _clean_map.)
_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064"
                       "\ufeff\u180e")
_SOFT_HYPHEN = "\u00ad"
# ff fi fl ffi ffl, long s + t, s + t
_LIGATURE_LETTERS = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
                     "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st",
                     "\ufb06": "st"}
# A broken PDF font mapping truncates the mathematical letters (U+1D400 -
# U+1D7FF: italic beta, script L, ...) to 16 bits, which lands them in the
# Hangul syllables block: U+1D6FD reads U+D6FD.
_HANGUL_FAULT = re.compile("[\ud400-\ud7ff]")
# Hangul that no such fault produces: the jamo, the compatibility jamo, the
# extended jamo and the syllables below U+D400.
_HANGUL_OTHER = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud3ff]")
# Anything clean_text could change: outside printable ASCII, tab and newline.
_PLAIN = re.compile(r"[^\x09\x0a\x20-\x7e]")


def is_korean(text: str) -> bool:
    """Is this document Korean? A real Korean text has most of its Hangul
    outside U+D400-U+D7FF; a font fault puts all of it inside."""
    other = len(_HANGUL_OTHER.findall(text or ""))
    if other < 3:
        return False
    faulty = len(_HANGUL_FAULT.findall(text))
    return other >= 0.25 * (other + faulty)


def _hangul_word(text: str, at: int) -> bool:
    return 0 <= at < len(text) and bool(_HANGUL_OTHER.match(text[at]))


def _clean_map(text: str, korean: bool | None = None) -> tuple[str, list[int]]:
    """clean_text, plus for every output character the index of the input
    character it came from - so a PDF page cleaned the way its stored text
    was can be matched against it and its boxes still found."""
    if korean is None:
        korean = is_korean(text)
    out: list[str] = []
    index: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        code = ord(ch)
        if code < 0x20 or 0x7f <= code <= 0x9f:  # control characters
            if ch in "\n\t":
                out.append(ch)
                index.append(i)
            elif ch == "\r" and not (i + 1 < n and text[i + 1] == "\n"):
                out.append("\n")  # a line break all the same
                index.append(i)
            i += 1
            continue
        if ch == _SOFT_HYPHEN:
            # A soft hyphen is invisible - except at a line end, where it is
            # the hyphen the page shows.
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            if j == n or text[j] in "\n\r":
                out.append("-")
                index.append(i)
            i += 1
            continue
        if ch in _INVISIBLE:
            i += 1
            continue
        if ch in _WIDE_SPACES:
            out.append(" ")
            index.append(i)
            i += 1
            continue
        letters = _LIGATURE_LETTERS.get(ch)
        if letters:
            out.extend(letters)
            index.extend([i] * len(letters))
            i += 1
            continue
        if 0xD400 <= code <= 0xD7FF and not korean:
            j = i
            while j < n and 0xD400 <= ord(text[j]) <= 0xD7FF:
                j += 1
            # A run next to undoubted Hangul is a Korean word, fault or not.
            keep = _hangul_word(text, i - 1) or _hangul_word(text, j)
            for k in range(i, j):
                out.append(text[k] if keep else chr(ord(text[k]) + 0x10000))
                index.append(k)
            i = j
            continue
        out.append(ch)
        index.append(i)
        i += 1
    return "".join(out), index


def clean_text(text: str, *, korean: bool | None = None) -> str:
    """A text layer's text as a reader should get it - every character that
    is the text kept, every one that only gets in the way of reading,
    searching or copying a quote taken out:

    - non-breaking, thin, hair, en, em, narrow and ideographic spaces become
      a plain space;
    - soft hyphens, zero-width characters, word joiners, the byte-order mark
      and control characters (other than newline and tab) go - a soft hyphen
      at a line end becomes the "-" the page shows there;
    - ligatures are expanded (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ);
    - mathematical letters a broken font mapping truncated into the Hangul
      block (U+D6FD for U+1D6FD, italic beta) are put back, unless the
      document is Korean: decided per document by `korean`, or from this
      text when None (see is_korean);
    - curly quotes, real dashes, superscripts, Greek and maths are the text
      and are left alone.

    Idempotent: cleaning a clean text changes nothing.
    """
    if not text:
        return text or ""
    if not _PLAIN.search(text):
        return text
    return _clean_map(text, korean)[0]


# ============================================================ matching

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
              "ﬃ": "ffi", "ﬄ": "ffl", "’": "'", "‘": "'",
              "“": '"', "”": '"', "–": "-", "—": "-"}


def normalize(text: str) -> str:
    """Collapse whitespace, fix ligatures, strip end-of-line hyphenation."""
    for lig, repl in _LIGATURES.items():
        text = text.replace(lig, repl)
    text = re.sub(r"-\s*\n\s*", "", text)  # exam-\nple -> example
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def loose(text: str) -> str:
    """A form for "is this quote in that text at all?" checks: normalized,
    lower-cased, markdown emphasis and punctuation spacing dropped."""
    text = normalize(text).lower()
    text = re.sub(r"[*_`#>|]", "", text)
    text = re.sub(r"\s*([,.;:!?()\[\]])\s*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 20]


# The highlight of the one citation a reader clicked, among the yellow rest.
FOCUS_COLOUR = (1.0, 0.55, 0.0)


def _highlight(doc, idx: int, rects, colour: tuple | None = None) -> None:
    """One highlight annotation. The rationale is not put in its contents:
    a viewer draws a note icon for that, over the passage's own words, and
    shows the rationale in its sidebar anyway."""
    # Keep the Page object alive while touching the annotation: PyMuPDF
    # annotations become unbound when their parent page is garbage-collected.
    page = doc[idx]
    annot = page.add_highlight_annot(rects)
    annot.set_info(title="LitReview")
    if colour is not None:
        annot.set_colors(stroke=colour)
    annot.update()


def _as_rect(rect) -> tuple:
    return (rect[0], rect[1], rect[2], rect[3])


_MARKUP = set("*_`#>|")
# A stored text's <!-- page N --> markers, and the <page N> ... </page N>
# tags a task's text is given in: neither is the text's.
_PAGE_MARKER = re.compile(r"<!-- page \d+ -->|</?page \d+>")
_DASHES = set("-‐‑‒–—―−")
_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'",
           "“": '"', "”": '"', "„": '"', "‟": '"',
           "′": "'", "″": '"', "´": "'", "`": "'"}


def _fold(ch: str) -> str:
    """One character as matching sees it: compatibility forms resolved (so
    ligatures, non-breaking and thin spaces, full-width and math-italic
    letters become their plain selves), curly quotes straightened, lower
    case. Math letters a broken PDF font mapping truncated into the Hangul
    block (U+1D6FD read as U+D6FD) are put back first - on both sides of a
    match, so a real Korean text still matches itself."""
    code = ord(ch)
    if 0xD400 <= code <= 0xD7FF:
        ch = chr(code + 0x10000)
    ch = _QUOTES.get(ch, ch)
    return unicodedata.normalize("NFKC", ch).lower()


def _ends_line(text: str, i: int) -> bool:
    """Does a line break (or the end of the text) follow position i, past
    any spaces and tabs?"""
    j = i + 1
    n = len(text)
    while j < n and text[j] in " \t":
        j += 1
    return j == n or text[j] == "\n"


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Text normalized for matching, plus, per output character, its index in
    the input.

    What matching ignores: case; ligatures and compatibility forms; curly vs
    straight quotes; invisible characters (soft hyphens, zero-width spaces,
    control characters); markdown emphasis; runs of whitespace; and hyphens
    inside words - "log-likelihood", "log-\\nlikelihood" and "loglikelihood"
    are one word to it, because a PDF's line-end hyphen cannot be told from a
    real one. A soft hyphen at a line end counts as such a hyphen: it is the
    one the page shows.
    """
    out: list[str] = []
    index: list[int] = []
    n = len(text)
    i = 0
    prev_space = False
    markers = {m.start(): m.end() for m in _PAGE_MARKER.finditer(text)}
    while i < n:
        if i in markers:  # <!-- page N --> is not text: a space, at most
            if not prev_space and out:
                out.append(" ")
                index.append(i)
                prev_space = True
            i = markers[i]
            continue
        ch = text[i]
        category = unicodedata.category(ch)
        line_end_hyphen = ch == _SOFT_HYPHEN and _ends_line(text, i)
        escape = ch == "\\" and i + 1 < n and text[i + 1] in _MARKUP  # "\#"
        if escape or not line_end_hyphen and (
                ch in _MARKUP or (category in ("Cf", "Cc") and not ch.isspace())):
            i += 1
            continue
        if ch in _DASHES or line_end_hyphen:
            # A hyphen between two letters or digits, line break or not, is
            # dropped; any other dash is kept as a plain "-".
            j = i + 1
            while j < n and (text[j].isspace() or unicodedata.category(text[j]) == "Cf"):
                j += 1
            before = out[-1] if out else ""
            if before.isalnum() and j < n and text[j].isalnum() and (
                    j == i + 1 or "\n" in text[i + 1:j]):
                i = j
                continue
            out.append("-")
            index.append(i)
            prev_space = False
            i += 1
            continue
        if ch.isspace():
            if not prev_space and out:
                out.append(" ")
                index.append(i)
                prev_space = True
            i += 1
            continue
        for folded in _fold(ch):
            if folded.isspace():
                if not prev_space and out:
                    out.append(" ")
                    index.append(i)
                    prev_space = True
                continue
            out.append(folded)
            index.append(i)
            prev_space = False
        i += 1
    while out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


@functools.lru_cache(maxsize=4)
def _normalized(text: str) -> tuple[str, array.array]:
    """_norm_map of a whole stored text, kept for the next few lookups: a
    paper's text is matched against once for every finding in it."""
    norm, index = _norm_map(text)
    return norm, array.array("i", index)


def _display(span: str, quote: str) -> str:
    """The source passage as it should be shown and cited: its own words,
    whitespace collapsed, invisible characters gone, ligatures resolved,
    heading markers dropped, and each line-end hyphen kept or joined the way
    the quote has it."""
    wanted = _norm_keep_hyphens(quote)

    def join(match: re.Match) -> str:
        left, right = match.group(1), match.group(2)
        return (f"{left}-{right}" if f"{left}-{right}".lower() in wanted
                else f"{left}{right}")

    # A soft hyphen at a line end is the hyphen the page shows there.
    text = re.sub(_SOFT_HYPHEN + r"(?=[ \t]*(?:\n|$))", "-", span)
    text = "".join(ch for ch in text
                   if not (unicodedata.category(ch) in ("Cf", "Cc") and not ch.isspace()))
    text = re.sub(r"(\w+)-[ \t]*\n\s*(\w+)", join, text)
    for lig, repl in _LIGATURE_LETTERS.items():
        text = text.replace(lig, repl)
    text = _PAGE_MARKER.sub(" ", text)
    text = re.sub(r"(^|\n)[ \t]*#{1,6}[ \t]+", r"\1", text)  # markdown headings
    # ... and a "#" the text has at a line start, escaped so as not to be one.
    text = re.sub(r"(^|\n)([ \t]*)\\#", r"\1\2#", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)  # markdown bold
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def closest_passage(content: str, quote: str) -> tuple[str, int | None] | None:
    """The stretch of a text that comes closest to a quote that is not in it,
    and the page it starts on - offered back with a refusal, so a near-miss
    can be corrected rather than retyped.

    Candidates run between sentence ends and line starts (a title or a
    heading has no full stop), are about the quote's length, and are ranked
    by difflib's ratio, which respects word order.
    """
    import bisect
    import difflib

    from .paper_texts import page_at

    target = _norm_map(quote or "")[0][:600]
    size = len(target)
    if size < 15:
        return None
    norm, index = _normalized(content or "")
    if not norm:
        return None
    bounds = {0, len(norm)}
    bounds.update(m.end() for m in re.finditer(r"[.!?] ", norm))
    for m in re.finditer(r"\n", content or ""):
        bounds.add(bisect.bisect_left(index, m.end()))
    bounds = sorted(b for b in bounds if 0 <= b <= len(norm))
    best, best_ratio = None, 0.0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)
    for i, start in enumerate(bounds):
        for j in range(i + 1, min(i + 10, len(bounds))):
            stop = bounds[j]
            candidate = norm[start:stop].strip()
            if len(candidate) < size * 0.5:
                continue
            if len(candidate) > size * 1.8:
                break
            matcher.set_seq1(candidate)
            if (matcher.real_quick_ratio() <= best_ratio
                    or matcher.quick_ratio() <= best_ratio):
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best, best_ratio = (start, stop), ratio
    if best is None or best_ratio < 0.6:
        return None
    lo = index[min(best[0], len(index) - 1)]
    hi = index[min(best[1], len(index)) - 1] + 1
    return _display(content[lo:hi], quote), page_at(content, lo)


def _norm_keep_hyphens(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def match_quote(content: str, quote: str) -> dict | None:
    """Find a whole quote in a stored text. Strict: every character of the
    quote, as normalized for matching, must be there in order - a quote that
    is right for its first half and invented for its second does not match.

    Returns {"start", "end", "text"}: the character range in `content`, and
    the passage as the source has it (see _display) - what is stored and
    cited, so a quote is the paper's words even when the one given differed
    in spacing, ligatures or hyphenation. None when the quote is not there.
    """
    norm_content, index = _normalized(content or "")
    norm_quote, _ = _norm_map(quote or "")
    if len(norm_quote) < 8:
        return None
    at = norm_content.find(norm_quote)
    if at < 0:
        return None
    start = index[at]
    end = index[at + len(norm_quote) - 1] + 1
    return {"start": start, "end": end,
            "text": _display(content[start:end], quote)}


def locate_in_text(content: str, quote: str) -> tuple[int, int] | None:
    """Character range of a whole quote in a stored text (see match_quote)."""
    found = match_quote(content, quote)
    return (found["start"], found["end"]) if found else None


def _spans(content: str, quote: str, limit: int = 50) -> list[tuple[int, int]]:
    """Every place a whole quote is in a stored text, matched as
    match_quote matches."""
    if not content or not quote:
        return []
    norm_content, index = _normalized(content)
    norm_quote, _ = _norm_map(quote)
    if len(norm_quote) < 8:
        return []
    found = []
    at = norm_content.find(norm_quote)
    while at >= 0 and len(found) < limit:
        found.append((index[at], index[at + len(norm_quote) - 1] + 1))
        at = norm_content.find(norm_quote, at + 1)
    return found


def _best_span(content: str | None, quote: str,
               page: int | None = None) -> tuple[int, int] | None:
    """Where a quote is in a stored text - when it is there more than once
    (an abstract repeated in a conclusion), the place on or nearest its page."""
    spans = _spans(content or "", quote)
    if not spans:
        return None
    if len(spans) == 1 or not page:
        return spans[0]
    from .paper_texts import page_at

    return min(spans, key=lambda s: abs((page_at(content, s[0]) or page) - page))


def _original(text: str, index, lo: int, hi: int) -> str:
    """The stretch of `text` that normalized characters lo..hi-1 came from,
    as it stands there - its own characters and case - with page markers
    dropped and whitespace collapsed."""
    hi = min(hi, len(index))
    if lo >= hi:
        return ""
    span = text[index[lo]:index[hi - 1] + 1]
    span = re.sub(r"(^|\n)#{1,6}\s+", r"\1", _PAGE_MARKER.sub(" ", span))
    return re.sub(r"\s+", " ", span).strip()


def diagnose_miss(content: str, quote: str) -> str:
    """Why a quote did not match, for the refusal: how much of it is in the
    text before it parts company with the source, and where - counted in the
    quote's own characters, and both sides shown as they are written."""
    norm_content, content_index = _normalized(content or "")
    norm_quote, quote_index = _norm_map(quote or "")
    if not norm_quote:
        return "the quote is empty"
    lo, hi = 0, len(norm_quote)
    while lo < hi:  # the longest opening stretch the text contains
        mid = (lo + hi + 1) // 2
        if norm_content.find(norm_quote[:mid]) >= 0:
            lo = mid
        else:
            hi = mid - 1
    if lo < 12:
        return "it does not appear in the text at all"
    if lo >= len(norm_quote):
        return "it is in the text as given"
    at = norm_content.find(norm_quote[:lo])
    first = quote_index[0]
    total = quote_index[-1] + 1 - first
    said = _original(quote, quote_index, lo, lo + 50)
    source = _original(content, content_index, at + lo, at + lo + 50)
    where = f"the text has \"{source}\"" if source else "the text ends"
    return (f"its first {quote_index[lo] - first} of {total} characters match the "
            f"text, then it reads \"{said}\" where {where}")


def context_of(content: str, start: int, end: int, around: int = 220) -> str:
    """About a sentence either side of a passage, whitespace collapsed."""
    lo = max(0, start - around)
    hi = min(len(content), end + around)
    before = content[lo:start]
    after = content[end:hi]
    # Start and stop at sentence boundaries where there is one nearby.
    cut = max(before.rfind(". "), before.rfind("\n\n"))
    if cut >= 0:
        before = before[cut + 1:]
    stop = min([p for p in (after.find(". "), after.find("\n\n")) if p >= 0],
               default=-1)
    if stop >= 0:
        after = after[:stop + 1]
    joined = (("…" if lo < start and cut < 0 else "") + before + content[start:end]
              + after + ("…" if hi > end and stop < 0 else ""))
    joined = _PAGE_MARKER.sub(" ", joined)
    return re.sub(r"\s+", " ", joined).strip()


# ================================================= finding it in the PDF

class _PageText:
    """One PDF page's characters as extraction reads them (text_flags),
    cleaned as its stored text was (clean_text) and normalized as matching
    sees them (_norm_map) - each normalized character traced back to the box
    it is drawn in.

    The Hangul repair is left out (korean=True): _fold puts the mathematical
    letters back on both sides of a match anyway, so a repaired stored text
    and an unrepaired page agree.
    """

    def __init__(self, page):
        chars: list[str] = []
        self.boxes: list = []
        self.lines: list[int] = []
        line_no = 0
        data = page.get_text("rawdict", flags=text_flags())
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        for c in char.get("c") or "":
                            chars.append(c)
                            self.boxes.append(char.get("bbox"))
                            self.lines.append(line_no)
                chars.append("\n")
                self.boxes.append(None)
                self.lines.append(line_no)
                line_no += 1
        self.chars = "".join(chars)
        cleaned, from_raw = _clean_map(self.chars, korean=True)
        self.norm, from_clean = _norm_map(cleaned)
        self.to_raw = [from_raw[k] for k in from_clean]

    def boxes_between(self, lo: int, hi: int) -> list[tuple]:
        """One box per text line for normalized characters lo..hi-1,
        covering everything from the first to the last of them - a line-end
        hyphen the match skipped included."""
        if lo >= hi:
            return []
        per_line: dict[int, list[float]] = {}
        for k in range(self.to_raw[lo], self.to_raw[hi - 1] + 1):
            box = self.boxes[k]
            if box is None or self.chars[k].isspace():
                continue
            x0, y0, x1, y1 = box
            if x1 < x0 or y1 < y0:
                continue
            have = per_line.get(self.lines[k])
            if have is None:
                per_line[self.lines[k]] = [x0, y0, x1, y1]
            else:
                have[0], have[1] = min(have[0], x0), min(have[1], y0)
                have[2], have[3] = max(have[2], x1), max(have[3], y1)
        return [tuple(b) for b in per_line.values()]


def _pick(hay: str, needle: str, where: float | None) -> int | None:
    """Where a needle is in a page's normalized text: the occurrence nearest
    `where` (its relative position in the stored page) when there are
    several, else the first."""
    at = hay.find(needle)
    if at < 0:
        return None
    if where is None:
        return at
    best, gap = at, abs(at / max(1, len(hay)) - where)
    at = hay.find(needle, at + 1)
    while at >= 0:
        if abs(at / len(hay) - where) < gap:
            best, gap = at, abs(at / len(hay) - where)
        at = hay.find(needle, at + 1)
    return best


def _needle(text: str) -> str:
    """A passage as the page search sees it: cleaned like a stored text,
    normalized like matching."""
    return _norm_map(_clean_map(text, korean=True)[0])[0]


class _Locator:
    """Finds passages on a document's pages, reading each page once."""

    def __init__(self, doc):
        self.doc = doc
        self.pages: dict[int, _PageText | None] = {}

    def page(self, index: int) -> _PageText | None:
        if not 0 <= index < len(self.doc):
            return None
        if index not in self.pages:
            try:
                self.pages[index] = _PageText(self.doc[index])
            except Exception as exc:  # one broken page must not stop the rest
                logger.warning("Could not read page %d to highlight it: %s",
                               index + 1, exc)
                self.pages[index] = None
        return self.pages[index]

    def find(self, needle: str, index: int, where: float | None = None, *,
             everywhere: bool = False) -> dict[int, list] | None:
        """{page index: boxes} for a normalized passage: looked for on the
        page it should start on, then its neighbours (a page number can be
        off by one where a passage runs across a break), then across each
        break between them - and with `everywhere`, on every other page."""
        if len(needle) < 8:
            return None
        near = [index, index - 1, index + 1]
        for idx in near:
            page = self.page(idx)
            at = _pick(page.norm, needle, where if idx == index else None) if page else None
            if at is not None:
                return {idx: page.boxes_between(at, at + len(needle))}
        for first in (index, index - 1):
            found = self._across(first, needle)
            if found:
                return found
        if everywhere:
            for idx in sorted(range(len(self.doc)), key=lambda i: abs(i - index)):
                page = self.page(idx) if idx not in near else None
                at = _pick(page.norm, needle, None) if page else None
                if at is not None:
                    return {idx: page.boxes_between(at, at + len(needle))}
        return None

    def _across(self, first: int, needle: str) -> dict[int, list] | None:
        """A passage that starts on one page and ends on the next."""
        a, b = self.page(first), self.page(first + 1)
        if a is None or b is None or not a.norm or not b.norm:
            return None
        cut = len(a.norm)
        joined = a.norm + " " + b.norm
        at = joined.find(needle)
        while at >= 0:
            end = at + len(needle)
            if at < cut and end > cut + 1:
                return {first: a.boxes_between(at, cut),
                        first + 1: b.boxes_between(0, end - cut - 1)}
            at = joined.find(needle, at + 1)
        return None

    def by_sentence(self, text: str, index: int) -> dict[int, list] | None:
        """What can be placed of a passage one sentence at a time."""
        placed: dict[int, list] = {}
        for sentence in _sentences(text):
            for idx, boxes in (self.find(_needle(sentence), index) or {}).items():
                placed.setdefault(idx, []).extend(boxes)
        return placed or None


def _page_number(value, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _where(content: str, start: int, page: int) -> float | None:
    """How far into its page (0-1) a passage starts in the stored text."""
    from .paper_texts import page_offset

    begin = page_offset(content, page)
    if begin is None or begin > start:
        return None
    following = _PAGE_MARKER.search(content, start)
    stop = following.start() if following else len(content)
    return (start - begin) / max(1, stop - begin)


def locate_and_highlight(doc, findings: list[dict],
                         content: str | None = None) -> list[dict]:
    """Highlight each finding in the open document; return the findings with
    `matched` and `rects` added and `page` set to where the highlight starts.
    Unmatched quotes are reported, never dropped.

    Given the paper's stored text (`content`), which came from this PDF, a
    quote is first found there and the source's own span is what is looked
    for, on the page the text says it starts on. Without it - or for a quote
    the text does not have - the quote itself is looked for, from the page
    Claude gave and then on every page.
    """
    from .paper_texts import page_at

    locator = _Locator(doc)
    last = max(0, len(doc) - 1)
    results = []
    for f in findings:
        quote = f.get("quote") or ""
        page = _page_number(f.get("page"))
        span = _best_span(content, quote, page) if content else None
        if span is not None:
            page = page_at(content, span[0]) or page
            text, where = content[span[0]:span[1]], _where(content, span[0], page)
        else:
            text, where = quote, None
        index = min(page - 1, last)
        placed = None
        if text.strip():
            placed = (locator.find(_needle(text), index, where,
                                   everywhere=span is None)
                      or locator.by_sentence(text, index))
        boxes = []
        for idx in sorted(placed or {}):
            if placed[idx]:
                _highlight(doc, idx, placed[idx])
                boxes.extend([idx, *map(float, _as_rect(r))] for r in placed[idx])
        if boxes:
            page = int(boxes[0][0]) + 1
        result = {"title": f.get("title") or "", "quote": quote, "page": page,
                  "why": f.get("why") or "", "matched": bool(boxes), "rects": boxes}
        if f.get("added_by"):
            result["added_by"] = f["added_by"]
        results.append(result)
    return results


# ================================================= what the text says of it

def _printed(finding: dict, page_labels: dict | None) -> None:
    """Set the finding's printed page from the paper's page labels (see
    paper_texts.detect_page_labels), or drop a stale one."""
    if page_labels is None:
        return
    page = finding.get("page")
    label = (page_labels.get(str(page)) or page_labels.get(page)) if page else None
    if label:
        finding["printed_page"] = str(label)
    else:
        finding.pop("printed_page", None)


def enrich_from_text(findings: list[dict], content: str | None,
                     page_at, section_at, page_labels: dict | None = None) -> list[dict]:
    """Add located/section/context to findings, from the paper's stored text,
    and printed_page from its page labels when they are known.

    located says how far a quote could be trusted to be where it claims:
    "pdf" - found and highlighted in the PDF; "text" - not highlighted, but
    present in the stored text (a scan Claude transcribed, or a PDF whose
    text layer the search could not see through), with its page taken from
    the text; "none" - found nowhere, so its page is unreliable.
    """
    enriched = []
    for f in findings:
        f = dict(f)
        span = (_best_span(content, f.get("quote") or "", _page_number(f.get("page")))
                if content else None)
        if f.get("matched"):
            f["located"] = "pdf"
        elif span is not None:
            f["located"] = "text"
            page = page_at(content, span[0])
            if page:
                f["page"] = page
        else:
            f["located"] = "none"
        if span is not None:
            f["section"] = section_at(content, span[0])
            f["context"] = context_of(content, *span)
        _printed(f, page_labels)
        enriched.append(f)
    return enriched


def refresh_findings(findings: list[dict], content: str | None,
                     page_labels: dict | None = None) -> list[dict]:
    """Findings re-read against a paper's new text (Claude's parse replacing
    the text layer's, say): located, section, context and printed_page are
    worked out again; the quotes and the highlights' geometry are not
    touched.

    A highlighted finding stays "pdf" and keeps the page its highlight is
    on. One that is not is "text" if the new text has it (its page then
    taken from there) and "none" if not. Section and context come from the
    new text where it has the quote; where it does not, the old ones - still
    the paper's own words around it - are kept.
    """
    from .paper_texts import page_at, section_at

    refreshed = []
    for f in findings or []:
        f = dict(f)
        before = f.get("page")
        span = (_best_span(content, f.get("quote") or "", _page_number(before))
                if content else None)
        if f.get("rects") or f.get("matched"):
            f["located"] = "pdf"
            if f.get("rects"):
                f["page"] = min(int(r[0]) for r in f["rects"]) + 1
        elif span is not None:
            f["located"] = "text"
            f["page"] = page_at(content, span[0]) or before
        else:
            f["located"] = "none"
        if span is not None:
            f["section"] = section_at(content, span[0])
            f["context"] = context_of(content, *span)
        if page_labels is not None:
            _printed(f, page_labels)
        elif f.get("page") != before:
            f.pop("printed_page", None)
        refreshed.append(f)
    return refreshed


# ======================================================== the PDF copies

def highlight_annotation(slug: str, paper_id: int, annotation_id: int,
                         findings: list[dict], *,
                         content: str | None = None) -> list[dict]:
    """Locate findings in the paper's original PDF and write the annotation's
    highlighted copy. Returns the findings with matched/rects (and page where
    the highlight starts). `content` is the paper's stored text: with it,
    each quote is placed from the source's own span (see
    locate_and_highlight). Without the original PDF, the findings are kept
    unmatched."""
    import pymupdf as fitz

    source = pdfs.original_path(slug, paper_id)
    if not source.exists() or not findings:
        return [dict(f, matched=False, rects=[]) for f in findings]
    with fitz.open(source) as doc:
        results = locate_and_highlight(doc, findings, content=content)
        dest = pdfs.annotation_path(slug, annotation_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.pdf")
        doc.save(str(tmp))
    tmp.replace(dest)
    return results


def _draw(doc, findings: list[dict], focus: dict | None = None) -> None:
    """Draw findings' stored highlight geometry - `focus`, if given, in
    FOCUS_COLOUR and the rest in the usual yellow."""
    import pymupdf as fitz

    for finding in findings:
        by_page: dict[int, list] = {}
        for idx, x0, y0, x1, y1 in finding.get("rects") or []:
            by_page.setdefault(int(idx), []).append(fitz.Rect(x0, y0, x1, y1))
        for idx, rects in by_page.items():
            if 0 <= idx < len(doc):
                _highlight(doc, idx, rects, FOCUS_COLOUR if finding is focus else None)


def rebuild_highlighted(annotation, paper, focus_key: str | None = None) -> Path | None:
    """Redraw one annotation's highlights onto a fresh copy of the original,
    when its file is missing. With `focus_key`, the copy that singles out
    that citation instead (see focused_copy)."""
    if focus_key:
        return focused_copy(annotation, paper, focus_key)
    import pymupdf as fitz

    source = pdfs.original_path(paper.project.slug, paper.id)
    findings = [f for f in (annotation.findings or []) if f.get("rects")]
    if not source.exists() or not findings:
        return None
    dest = pdfs.annotation_path(paper.project.slug, annotation.id)
    with fitz.open(source) as doc:
        _draw(doc, findings)
        dest.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dest))
    return dest


def focused_path(project_slug: str, annotation_id: int, key: str) -> Path:
    """Where the copy singling out one citation of an annotation is kept:
    beside the annotation's own copy (a12.pdf -> a12-focus-12-ab34cd56.pdf)."""
    own = pdfs.annotation_path(project_slug, annotation_id)
    return own.with_name(f"{own.stem}-focus-{re.sub(r'[^0-9A-Za-z-]', '', key or '')[:64]}.pdf")


def focused_copy(annotation, paper, key: str) -> Path | None:
    """A copy of one annotation's highlights in which the finding cited as
    `key` (citations.finding_key) is drawn orange and the rest yellow - the
    citation a reader clicked singled out among the others.

    None when the original PDF is missing or no highlighted finding has that
    key (serve the ordinary copy then). The copy is a derived file like the
    ordinary one: kept, and drawn again once the findings have changed.
    """
    import pymupdf as fitz

    from . import citations, models

    source = pdfs.original_path(paper.project.slug, paper.id)
    findings = [f for f in (annotation.findings or []) if f.get("rects")]
    focus = next((f for f in findings
                  if citations.finding_key(annotation.id, f) == key), None)
    if focus is None or not source.exists():
        return None
    dest = focused_path(paper.project.slug, annotation.id, key)
    changed = models.aware(annotation.updated_at)
    if (dest.exists() and changed is not None
            and dest.stat().st_mtime >= changed.timestamp()):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex[:8]}.tmp.pdf")
    with fitz.open(source) as doc:
        _draw(doc, findings, focus)
        doc.save(str(tmp))
    try:
        tmp.replace(dest)
    except OSError as exc:  # the old copy is being served right now (Windows)
        logger.warning("Could not replace %s (%s) - serving it as it was", dest, exc)
        tmp.unlink(missing_ok=True)
        return dest if dest.exists() else None
    return dest
