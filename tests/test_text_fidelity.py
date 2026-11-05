"""How faithfully a PDF's text is read, and how surely a quote is found again.

The PDFs here are built so that their text layer says exactly what real
papers' text layers say: ligature glyphs, curly quotes, non-breaking, thin
and hair spaces, soft hyphens, zero-width spaces, a glyph with no Unicode at
all (a control character), and mathematical letters a broken font map
truncated into the Hangul block. Two "papers" carry them the way real
economics papers do - one in a single column with running headers
numbering its pages from 71, one in two columns with page labels from 233 -
and every quote a reader would take from them must come back highlighted, in
the right place.
"""
import pymupdf as fitz
import pytest

from litreview_local import highlight, models, paper_texts, pdfs, services
from litreview_local.db import SessionLocal

# Every special character by name: none is typed into this file.
NBSP, SHY, THIN, HAIR, EN_SPACE = chr(0xA0), chr(0xAD), chr(0x2009), chr(0x200A), chr(0x2002)
NARROW, IDEO, EM_SPACE = chr(0x202F), chr(0x3000), chr(0x2003)
ZWSP, ZWJ, WJ, BOM = chr(0x200B), chr(0x200D), chr(0x2060), chr(0xFEFF)
FF, FI, FL, FFI, FFL, LONG_ST, ST = (chr(c) for c in range(0xFB00, 0xFB07))
LQ, RQ, LSQ, APOS = chr(0x201C), chr(0x201D), chr(0x2018), chr(0x2019)
DASH, EMDASH = chr(0x2013), chr(0x2014)
BETA = chr(0x1D6FD)              # MATHEMATICAL ITALIC SMALL BETA
BETA_FAULT = chr(0xD6FD)         # ... as a broken font map truncates it
GLYPH_WITHOUT_UNICODE = chr(0x02)
PRIVATE_USE = chr(0xE000)
# What Page.search_for searches with unless told otherwise - and what the
# highlighter used to search with: ligatures kept, odd spaces kept, words
# broken at a line end joined (self-|employed reads "selfemployed").
OLD_SEARCH_FLAGS = (fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE
                    | fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_MEDIABOX_CLIP)


# ------------------------------------------------------------ building PDFs

def _utf16(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def _tounicode(mapping: dict[int, str]) -> bytes:
    entries = sorted(mapping.items())
    chunks = []
    for at in range(0, len(entries), 100):
        part = entries[at:at + 100]
        chunks.append(f"{len(part)} beginbfchar\n"
                      + "\n".join(f"<{code:02X}> <{_utf16(text)}>" for code, text in part)
                      + "\nendbfchar")
    return ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            + "\n".join(chunks)
            + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
            ).encode()


def build_pdf(path, pages, *, labels=None):
    """A PDF whose text layer says exactly what it is given.

    `pages`: per page, [(x, y, size, text, bold)] with y from the top of a
    612 x 792 page, drawn - and so read - in that order. A character outside
    printable ASCII gets a code of its own and a ToUnicode entry saying what
    it is, the way a real PDF carries a ligature glyph or a curly quote;
    a control character gets none, and reads as its raw code - a glyph the
    font cannot name.
    """
    codes: dict[str, int] = {}

    def code(ch: str) -> int:
        if ord(ch) < 0x20 or 0x20 <= ord(ch) < 0x7F:
            return ord(ch)
        if ch not in codes:
            codes[ch] = 0x80 + len(codes)
            assert codes[ch] <= 0xFF, "too many special characters for one PDF"
        return codes[ch]

    streams = []
    for items in pages:
        ops = []
        for x, y, size, text, bold in items:
            data = "".join(f"\\{code(ch):03o}" for ch in text)
            ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {x} {792 - y} Td "
                       f"({data}) Tj ET")
        streams.append("\n".join(ops).encode())
    mapping = {c: chr(c) for c in range(0x20, 0x7F)}
    mapping.update({c: ch for ch, c in codes.items()})
    doc = fitz.open()
    for _ in pages:
        doc.new_page(width=612, height=792)
    cmap = doc.get_new_xref()
    doc.update_object(cmap, "<< >>")
    doc.update_stream(cmap, _tounicode(mapping))
    fonts = {}
    for name, base in (("F1", "Helvetica"), ("F2", "Helvetica-Bold")):
        fonts[name] = doc.get_new_xref()
        doc.update_object(fonts[name], f"<< /Type /Font /Subtype /Type1 /BaseFont /{base}"
                          f" /Encoding /WinAnsiEncoding /ToUnicode {cmap} 0 R >>")
    for number, data in enumerate(streams):
        page = doc[number]
        content = doc.get_new_xref()
        doc.update_object(content, "<< >>")
        doc.update_stream(content, data)
        doc.xref_set_key(page.xref, "Contents", f"{content} 0 R")
        doc.xref_set_key(page.xref, "Resources", "<< /Font << "
                         + " ".join(f"/{n} {x} 0 R" for n, x in fonts.items()) + " >> >>")
    if labels:
        doc.set_page_labels(labels)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def layout(*blocks, header=(), footer=(), x=72, top=80):
    """One page: `blocks` of (size, bold, lines) set one under the other,
    `header` items drawn before them and `footer` items after."""
    items = [(hx, hy, 9, text, False) for hx, hy, text in header]
    y = top
    for size, bold, lines in blocks:
        for line in lines:
            items.append((x, y, size, line, bold))
            y += size * 1.3
        y += size * 1.2
    items += [(fx, fy, 9, text, False) for fx, fy, text in footer]
    return items


def columns(left, right, header=(), top=80):
    """A two-column page: the left column's blocks are read before the right's."""
    items = [(hx, hy, 9, text, False) for hx, hy, text in header]
    for x, blocks in ((54, left), (316, right)):
        items += layout(*blocks, x=x, top=top)
    return items


def body(*lines):
    return (10, False, list(lines))


# Enough ordinary text for a page that is not a scan (paper_texts.MIN_TEXT_CHARS).
FILLER = body("The rest of this page is ordinary running text about wages, hours and",
              "the institutions that set them, long enough to be read as a text layer.",
              "It says nothing a reader would quote, which is all it is here for.")


def bold(*lines):
    return (10, True, list(lines))


# ------------------------------------------------- paper A: one column, 71-76

PAPER_A = [
    layout((16, False, ["Public Sector Pay and the Taste", "for Discrimination"]),
           (11, False, ["Gary S. Becker and Anna Haschka"]),
           bold("Abstract"),
           body(f"We study the e{FF}ect of centralised wage setting on the gender pay gap in",
                f"Danish municipalities. The estimated e{FF}ect is statistically signi{FI}cant: the",
                f"gap falls by 5{NBSP}per{NBSP}cent when wages are bargained centrally, and by more",
                "for workers who were self-",
                f"employed before they joined the public sector. Employers{APOS} {LQ}taste for",
                f"discrimination{RQ} explains about a third of the remaining gap."),
           bold("1 Introduction"),
           body("Since Becker (1957), economists have asked whether competition drives",
                "out discrimination. In the public sector there is no product market to",
                "discipline employers, so the question is whether centralised bargain-",
                "ing does the work instead. We exploit a reform that moved wage setting",
                f"from the municipalities to a national board in 2007{THIN}{DASH}{THIN}2009. Our main",
                f"sample covers 98 municipalities and 412{THIN}000 employees."),
           footer=[(72, 760, f"Journal of Public Administration 62:71{DASH}90")]),
    layout(bold("2 Data and Institutional", "Background"),
           body("The data come from the registers of Statistics Denmark. We observe the",
                "full employment histories of all municipal workers, including their oc-",
                f"cupation, hours and the bargaining unit that sets their pay.{GLYPH_WITHOUT_UNICODE} We drop",
                "workers with fewer than 100 hours a year, which leaves a balanced panel.",
                f"Table{NBSP}2 reports the sample means; the elasticity {BETA_FAULT} is estimated",
                "from the within-municipality variation in bargaining coverage."),
           body(f"Coverage rose quickly after the re{SHY}form: by 2009 nearly all employ{SHY}",
                "ees were covered by the national agreement, and the share of pay set",
                f"lo{ZWSP}cally fell from 40 to 12 per cent."),
           header=[(72, 40, "72"), (200, 40, "JOURNAL OF PUBLIC ADMINISTRATION")]),
    layout(bold("3 Results"),
           body("Table 3 shows the main estimates. Centralised bargaining reduced the",
                f"gender gap by 4.8 log points (s.e. 1.1), an e{FF}ect that is robust to the",
                f"inclusion of municipality-speci{FI}c trends. The reduction is concentrated",
                "among the low-paid, where the gap closes almost entirely; among the high-",
                f"paid it is unchanged. We interpret this as evidence that {LQ}discretion{RQ}",
                f"rather than productivity sustained the gap {EMDASH} a {FI}nding in line with",
                f"Becker{APOS}s prediction that discrimination survives where it is not costly."),
           body("Our preferred estimate comes from a difference-in-",
                "differences design; it is 3.9 log points, with a confidence interval that",
                "excludes zero in every specification we try."),
           header=[(72, 40, "PUBLIC SECTOR PAY AND DISCRIMINATION"), (530, 40, "73")]),
    layout(body(f"Municipal o{FFI}cials had {FL}exible schedules, which made the reform easy",
                f"to implement. The pattern is ba{FFL}ing only if one ignores the a{FF}luent",
                f"suburbs, where the so-called {LSQ}wage drift{APOS} was largest in 2008."),
           (14, False, [chr(0xF2)]),
           (14, False, [chr(0xD5) + chr(0xD4) + chr(0xD2)]),
           body("Figure 1 plots the gap by decile. Above the median the reform had no",
                "measurable effect at all, a result we return to in the conclusion."),
           header=[(72, 40, "74"), (200, 40, "JOURNAL OF PUBLIC ADMINISTRATION")]),
    layout(bold("4 Conclusion"),
           body("Centralised bargaining narrowed the gender pay gap in Danish municipali-",
                "ties, and the narrowing was largest where managers had most discretion",
                "before the reform. This is what the taste model predicts, and it suggests",
                "that the remaining gap is not explained by differences in productivity",
                "alone. Whether the same holds in private firms, where competition can",
                "discipline employers directly, is a question for future work, and one"),
           header=[(72, 40, "PUBLIC SECTOR PAY AND DISCRIMINATION"), (530, 40, "75")]),
    layout(body("that the registers now make it possible to answer."),
           bold("References"),
           body("Becker, G. S. (1957). The Economics of Discrimination. University of",
                "Chicago Press."),
           footer=[(300, 760, "76")]),
]

# (the page a reader names, the quote as a reader types it)
QUOTES_A = [
    (1, "The estimated effect is statistically significant: the gap falls by 5 per "
        "cent when wages are bargained centrally"),
    (1, "and by more for workers who were self-employed before they joined the "
        "public sector."),
    (1, "Employers' \"taste for discrimination\" explains about a third of the "
        "remaining gap."),
    (1, "Since Becker (1957), economists have asked whether competition drives out "
        "discrimination."),
    (1, "so the question is whether centralised bargaining does the work instead."),
    (1, "We exploit a reform that moved wage setting from the municipalities to a "
        f"national board in 2007 {DASH} 2009."),
    (1, "Our main sample covers 98 municipalities and 412 000 employees."),
    (2, "The data come from the registers of Statistics Denmark."),
    (2, "We observe the full employment histories of all municipal workers, "
        "including their occupation, hours and the bargaining unit that sets their pay."),
    (2, "the bargaining unit that sets their pay. We drop workers with fewer than "
        "100 hours a year"),
    (2, f"Table 2 reports the sample means; the elasticity {chr(0x3B2)} is estimated "
        "from the within-municipality variation in bargaining coverage."),
    (2, "Coverage rose quickly after the reform: by 2009 nearly all employees were "
        "covered by the national agreement"),
    (2, "and the share of pay set locally fell from 40 to 12 per cent."),
    (3, "Centralised bargaining reduced the gender gap by 4.8 log points (s.e. 1.1), "
        "an effect that is robust to the inclusion of municipality-specific trends."),
    (3, "among the low-paid, where the gap closes almost entirely; among the "
        "high-paid it is unchanged."),
    (3, f"We interpret this as evidence that {LQ}discretion{RQ} rather than "
        f"productivity sustained the gap {EMDASH} a finding in line with Becker's prediction"),
    (3, "Our preferred estimate comes from a difference-in-differences design; it "
        "is 3.9 log points"),
    (4, "Municipal officials had flexible schedules, which made the reform easy to "
        "implement."),
    (4, "The pattern is baffling only if one ignores the affluent suburbs, where the "
        "so-called 'wage drift' was largest in 2008."),
    (4, "Above the median the reform had no measurable effect at all"),
    (5, "Centralised bargaining narrowed the gender pay gap in Danish municipalities, "
        "and the narrowing was largest where managers had most discretion before "
        "the reform."),
    (5, "Whether the same holds in private firms, where competition can discipline "
        "employers directly, is a question for future work, and one that the "
        "registers now make it possible to answer."),
    (6, "Becker, G. S. (1957). The Economics of Discrimination."),
]

# --------------------------------------- paper B: two columns, labels 233-236

_B_HEADER = [(200, 40, f"A. Haschka / Labour Economics 41 (2016) 233{DASH}252")]
PAPER_B = [
    [(54, 70, 14, "Minimum Wages and the Employment of", False),
     (54, 88, 14, "Young Workers: Evidence from Austria", False),
     (54, 112, 10, "Anna Haschka", False)]
    + columns(
        [(9, True, ["Abstract"]),
         (9, False, [f"We estimate the employment e{FF}ects of",
                     "the 2009 minimum wage increase for",
                     f"workers aged 16{DASH}24. Employment fell by",
                     f"2.1{NBSP}% among teenagers but not among",
                     f"young adults, a di{FF}erence that persists",
                     "for at least three years after the in-",
                     "crease and survives a placebo test on"])],
        [(9, False, ["older workers. The results are consis-",
                     "tent with a monopsony model only for",
                     f"the largest {FI}rms."]),
         (9, True, ["1 Introduction"]),
         (9, False, [f"The so-called {LQ}employment puzzle{RQ} has",
                     "occupied labour economists since Card",
                     f"and Krueger{APOS}s (1994) study of fast-food",
                     "restaurants in New Jersey."])],
        top=140),
    columns(
        [(9, True, [f"2 Data{NBSP}and Methods"]),
         (9, False, ["We use administrative records of the",
                     "Austrian social security system, which",
                     "cover the universe of private-sector",
                     "employment spells between 2005 and",
                     f"2014. Wages are top-coded at the max{SHY}",
                     f"imum contribution base, which a{FF}ects",
                     "fewer than 1% of the young workers."])],
        [(9, False, ["The treatment group consists of work-",
                     "ers whose wage in 2008 was below the",
                     "new minimum; the control group earned",
                     "slightly more. Both groups are followed",
                     "for 36 months."])],
        header=_B_HEADER),
    columns(
        [(9, True, ["3 Results"]),
         (9, False, ["Table 2 reports the main estimates.",
                     "The probability of remaining employed",
                     "fell by 2.1 percentage points (s.e. 0.6)",
                     f"for teenagers, a statistically signi{FI}cant",
                     f"effect, while it rose insigni{FI}cantly for",
                     f"those aged 20{DASH}24."])],
        [(9, False, [f"These e{FF}ects are concentrated in small",
                     f"{FI}rms, where the employer{APOS}s wage-setting",
                     f"power is weakest; in the largest {FI}rms",
                     f"employment was unchanged{EMDASH}as a monop-",
                     "sony model would predict."])],
        header=_B_HEADER),
    columns(
        [(9, True, ["4 Conclusion"]),
         (9, False, ["Minimum wages reduced the employment",
                     f"of teenagers in Austria, but the e{FF}ect is",
                     "small and confined to small firms."])],
        [(9, True, ["References"]),
         (9, False, ["Card, D., Krueger, A. B. (1994). Minimum",
                     "wages and employment. American",
                     f"Economic Review 84, 772{DASH}793."])],
        header=_B_HEADER),
]
LABELS_B = [{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 233}]

QUOTES_B = [
    (1, "We estimate the employment effects of the 2009 minimum wage increase for "
        f"workers aged 16{DASH}24."),
    (1, "Employment fell by 2.1 % among teenagers but not among young adults"),
    (1, "a difference that persists for at least three years after the increase and "
        "survives a placebo test on older workers."),
    (1, "The results are consistent with a monopsony model only for the largest firms."),
    (1, f"The so-called {LQ}employment puzzle{RQ} has occupied labour economists since "
        "Card and Krueger's (1994) study of fast-food restaurants in New Jersey."),
    (2, "We use administrative records of the Austrian social security system, which "
        "cover the universe of private-sector employment spells between 2005 and 2014."),
    (2, "Wages are top-coded at the maximum contribution base, which affects fewer "
        "than 1% of the young workers."),
    (2, "The treatment group consists of workers whose wage in 2008 was below the "
        "new minimum"),
    (3, "The probability of remaining employed fell by 2.1 percentage points (s.e. "
        "0.6) for teenagers, a statistically significant effect"),
    (3, f"while it rose insignificantly for those aged 20{DASH}24."),
    (3, "These effects are concentrated in small firms, where the employer's "
        "wage-setting power is weakest"),
    (3, f"in the largest firms employment was unchanged{EMDASH}as a monopsony model "
        "would predict."),
    (4, "Minimum wages reduced the employment of teenagers in Austria, but the "
        "effect is small and confined to small firms."),
]


@pytest.fixture(scope="module")
def paper_a(tmp_path_factory):
    return build_pdf(tmp_path_factory.mktemp("a") / "becker.pdf", PAPER_A)


@pytest.fixture(scope="module")
def paper_b(tmp_path_factory):
    return build_pdf(tmp_path_factory.mktemp("b") / "haschka.pdf", PAPER_B,
                     labels=LABELS_B)


def _covered(doc, rects) -> str:
    """The text under a highlight's boxes, in reading order."""
    by_page: dict[int, list] = {}
    for idx, *box in rects:
        by_page.setdefault(int(idx), []).append(fitz.Rect(box))
    chars = []
    for idx in sorted(by_page):
        data = doc[idx].get_text("rawdict", flags=highlight.text_flags())
        for block in data["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for char in span["chars"]:
                        centre = fitz.Rect(char["bbox"]).tl + (fitz.Rect(char["bbox"]).br
                                                               - fitz.Rect(char["bbox"]).tl) * 0.5
                        if any(centre in r for r in by_page[idx]):
                            chars.append(char["c"])
                chars.append("\n")
    return "".join(chars)


def _placed(path, quotes, *, with_text: bool):
    """Each quote through the pipeline: matched in the stored text (the
    source's own words kept), then highlighted. -> [(quote, stored, result)]"""
    content = paper_texts.compose(paper_texts.extract_pages(path))
    out = []
    with fitz.open(path) as doc:
        for page, typed in quotes:
            found = highlight.match_quote(content, typed)
            assert found is not None, f"not in the text: {typed!r}"
            result = highlight.locate_and_highlight(
                doc, [{"quote": found["text"], "page": page, "why": "w"}],
                content=content if with_text else None)[0]
            out.append((typed, found["text"], result))
    return content, out


# ------------------------------------------------------------- clean_text

def test_clean_text_turns_every_kind_of_space_into_a_space():
    for space in (NBSP, THIN, HAIR, EN_SPACE, EM_SPACE, NARROW, IDEO):
        assert highlight.clean_text(f"5{space}per cent") == "5 per cent"


def test_clean_text_removes_what_is_never_seen():
    text = (f"re{SHY}form lo{ZWSP}cally join{ZWJ}er word{WJ}joiner {BOM}start "
            f"a{GLYPH_WITHOUT_UNICODE}b c{chr(0x08)}d e{chr(0x7F)}f g{chr(0x92)}h")
    assert highlight.clean_text(text) == ("reform locally joiner wordjoiner start "
                                          "ab cd ef gh")
    assert highlight.clean_text("one\ttwo\nthree\r\nfour\rfive") == "one\ttwo\nthree\nfour\nfive"


def test_clean_text_keeps_a_soft_hyphen_where_the_line_breaks():
    assert highlight.clean_text(f"employ{SHY}\nees") == "employ-\nees"
    assert highlight.clean_text(f"employ{SHY}  \nees") == "employ-  \nees"
    assert highlight.clean_text(f"the end treat{SHY}") == "the end treat-"


def test_clean_text_expands_ligatures():
    assert (highlight.clean_text(f"e{FF}ect signi{FI}cant {FL}ow o{FFI}ce ba{FFL}ed "
                                 f"fa{LONG_ST} {ST}op")
            == "effect significant flow office baffled fast stop")


def test_clean_text_leaves_the_characters_that_are_the_text():
    text = (f"{LQ}quoted{RQ} {LSQ}single{APOS} don{APOS}t 1990{DASH}2000 a {EMDASH} b "
            f"x{chr(0xB2)} H{chr(0x2082)}O {chr(0x3B2)} {chr(0x2211)} {chr(0x2264)} {BETA} "
            f"{chr(0xE9)}t{chr(0xE9)} {PRIVATE_USE} fraction {chr(0xBD)}")
    assert highlight.clean_text(text) == text


def test_clean_text_repairs_maths_truncated_into_hangul_unless_the_text_is_korean():
    english = f"the elasticity {BETA_FAULT} and {chr(0xD465)}{chr(0xD466)} are estimated"
    assert highlight.clean_text(english) == (f"the elasticity {BETA} and "
                                             f"{chr(0x1D465)}{chr(0x1D466)} are estimated")
    korean = "한국의 노동시장에서 성별 임금 격차는 여전히 크다. 할 수 있다."
    assert highlight.is_korean(korean)
    assert highlight.clean_text(korean) == korean
    assert highlight.clean_text(english, korean=True) == english
    assert highlight.clean_text(korean, korean=False) != korean  # forced: 할, 히, 크 ...
    # A Korean word in an English paper is left alone even so: its in-range
    # syllable sits next to Hangul no font fault produces.
    mixed = f"Seoul ({chr(0xD55C)}{chr(0xAD6D)}) and the coefficient {BETA_FAULT}"
    assert highlight.clean_text(mixed) == (f"Seoul ({chr(0xD55C)}{chr(0xAD6D)}) and the "
                                           f"coefficient {BETA}")


def test_clean_text_is_idempotent():
    text = (f"e{FF}ect{NBSP}of the treat{SHY}\nment {BETA_FAULT}{ZWSP} "
            f"{LQ}q{RQ}{GLYPH_WITHOUT_UNICODE}")
    once = highlight.clean_text(text)
    assert highlight.clean_text(once) == once


# ------------------------------------------------------------- extraction

def test_the_flags_extraction_and_search_share():
    flags = highlight.text_flags()
    assert flags & fitz.TEXT_MEDIABOX_CLIP
    assert not flags & fitz.TEXT_PRESERVE_LIGATURES      # ligatures expanded
    assert not flags & fitz.TEXT_PRESERVE_WHITESPACE     # every space a space
    assert not flags & fitz.TEXT_DEHYPHENATE             # self-|employed stays


def test_extraction_gives_clean_text(paper_a):
    pages = paper_texts.extract_pages(paper_a)
    text = "\n".join(pages)
    for unwanted in (NBSP, SHY, THIN, ZWSP, BOM, GLYPH_WITHOUT_UNICODE, FF, FI, FL, FFI,
                     FFL, BETA_FAULT):
        assert unwanted not in text, f"U+{ord(unwanted):04X} left in the text"
    assert "statistically significant" in pages[0]
    assert "5 per cent" in pages[0]
    assert "412 000 employees" in pages[0]
    assert f"Employers{APOS} {LQ}taste for" in pages[0]          # curly quotes kept
    assert f"2007 {DASH} 2009" in pages[0]                        # the dash kept
    assert "for workers who were self-\nemployed" in pages[0]    # hyphen kept
    assert f"elasticity {BETA} is estimated" in pages[1]          # maths repaired
    assert "employ-\nees were covered" in pages[1]               # soft hyphen shown
    assert "reform: by 2009" in pages[1] and "locally fell" in pages[1]
    assert "pay. We drop" in pages[1]
    assert "officials had flexible" in pages[3] and "baffling" in pages[3]


def test_a_korean_paper_keeps_its_hangul(tmp_path):
    lines = ["한국의 노동시장에서 성별 임금 격차는 여전히 크다.",
             "최저임금 인상은 청년 고용을 줄일 수 있다는 우려가 있다.",
             f"할 수 있는 일은 많다. 계수 {BETA_FAULT} 의 추정치는 0.5 이다.",
             "This English sentence sits in a Korean paper and is long enough."] * 3
    path = build_pdf(tmp_path / "korean.pdf", [layout(body(*lines))])
    text = paper_texts.extract_pages(path)[0]
    assert "한국의 노동시장에서" in text and "할 수 있는" in text
    assert BETA_FAULT in text and BETA not in text  # decided per document


def test_a_heading_set_over_two_lines_is_one_heading(paper_a, paper_b):
    pages_a = paper_texts.extract_pages(paper_a)
    assert pages_a[0].startswith("# Public Sector Pay and the Taste for Discrimination\n")
    assert "### 2 Data and Institutional Background\n" in pages_a[1]
    pages_b = paper_texts.extract_pages(paper_b)
    assert pages_b[0].startswith("# Minimum Wages and the Employment of Young Workers: "
                                 "Evidence from Austria\n")
    assert "### 2 Data and Methods\n" in pages_b[1]  # the NBSP in it is gone
    content = paper_texts.compose(pages_a)
    headings = [h["heading"] for h in paper_texts_outline(content)]
    assert headings == ["Public Sector Pay and the Taste for Discrimination", "Abstract",
                        "1 Introduction", "2 Data and Institutional Background",
                        "3 Results", "4 Conclusion", "References"]


def paper_texts_outline(content):
    from litreview_local import documents

    return documents.outline(content)


def test_garbled_glyphs_are_not_headings(paper_a):
    page = paper_texts.extract_pages(paper_a)[3]
    assert f"\n{chr(0xF2)}\n" in f"\n{page}\n"          # kept as text ...
    assert f"# {chr(0xF2)}" not in page                 # ... not as a heading
    assert f"# {chr(0xD5)}" not in page


def test_heading_lines_are_merged_only_where_they_belong(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Remote Work, Wellbeing and the", fontsize=20)
    page.insert_text((72, 96), "Boundaries of the Working Day", fontsize=20)
    y = 130
    page.insert_text((72, y), "5 Robustness", fontsize=10, fontname="hebo")
    page.insert_text((72, y + 13), "5.1 Placebo tests", fontsize=10, fontname="hebo")
    page.insert_text((72, y + 40), "Results and", fontsize=10, fontname="hebo")
    page.insert_text((72, y + 53), "further discussion", fontsize=10, fontname="hebo")
    y += 80
    for _ in range(14):
        page.insert_text((72, y), "Body text about remote work and wellbeing, set in "
                         "the ordinary size of the paper.", fontsize=10)
        y += 14
    path = tmp_path / "headings.pdf"
    doc.save(str(path))
    doc.close()
    text = paper_texts.extract_pages(path)[0]
    assert "# Remote Work, Wellbeing and the Boundaries of the Working Day\n" in text
    assert "### 5 Robustness\n### 5.1 Placebo tests\n" in text  # numbered: two
    assert "### Results and further discussion\n" in text


# ------------------------------------------------------------ highlighting

def test_every_quote_is_highlighted_where_it_is(paper_a, paper_b):
    for path, quotes in ((paper_a, QUOTES_A), (paper_b, QUOTES_B)):
        content, placed = _placed(path, quotes, with_text=True)
        with fitz.open(path) as doc:
            for (page, typed), (_, stored, result) in zip(quotes, placed):
                assert result["matched"], f"not highlighted: {typed!r}"
                assert result["page"] == page, typed
                # The highlight covers the passage itself, nothing more.
                under = highlight._needle(_covered(doc, result["rects"]))
                assert under == highlight._needle(stored), typed


def test_quotes_are_highlighted_from_the_quote_alone_too(paper_a, paper_b):
    """Without the stored text (as research passes it today) the quote is
    looked for itself - still through ligatures, hyphens and curly quotes."""
    for path, quotes in ((paper_a, QUOTES_A), (paper_b, QUOTES_B)):
        _, placed = _placed(path, quotes, with_text=False)
        missed = [typed for typed, _, result in placed if not result["matched"]]
        assert not missed


def test_a_passage_across_a_page_break_is_highlighted_on_both_pages(paper_a):
    _, placed = _placed(paper_a, QUOTES_A[-2:-1], with_text=True)
    rects = placed[0][2]["rects"]
    assert {int(r[0]) for r in rects} == {4, 5}
    assert placed[0][2]["page"] == 5


def test_the_raw_span_is_found_by_search_for_with_the_same_flags(paper_a):
    """search_for agrees with extraction once both use the same flags: the
    source's span, whitespace collapsed, is found where the old search flags
    (which dehyphenate) lose a real hyphen at a line end."""
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    found = highlight.match_quote(content, QUOTES_A[1][1])
    raw = " ".join(content[found["start"]:found["end"]].split())
    assert "self- employed" in raw
    with fitz.open(paper_a) as doc:
        assert doc[0].search_for(raw, flags=highlight.text_flags())
        assert not doc[0].search_for(found["text"], flags=OLD_SEARCH_FLAGS)  # the old way


def test_a_repeated_passage_is_highlighted_where_the_text_has_it(tmp_path):
    sentence = "The gap closes when wages are set centrally."
    path = build_pdf(tmp_path / "twice.pdf", [
        layout(body(sentence, "Other words follow here on the first page."), FILLER),
        layout(body("A second page says something else entirely first.", sentence),
               FILLER)])
    content = paper_texts.compose(paper_texts.extract_pages(path))
    with fitz.open(path) as doc:
        result = highlight.locate_and_highlight(
            doc, [{"quote": sentence, "page": 2, "why": ""}], content=content)[0]
    assert result["page"] == 2
    assert {int(r[0]) for r in result["rects"]} == {1}
    assert len(result["rects"]) == 1  # one occurrence, not every one


def test_unplaceable_quotes_are_kept_and_reported(paper_a):
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    with fitz.open(paper_a) as doc:
        result = highlight.locate_and_highlight(
            doc, [{"quote": "Nothing like this sentence is anywhere in the paper.",
                   "page": 2, "why": "w", "title": "t"}], content=content)[0]
    assert result == {"title": "t", "quote": "Nothing like this sentence is anywhere "
                      "in the paper.", "page": 2, "why": "w", "matched": False,
                      "rects": []}


def test_highlight_annotation_writes_the_copy_and_redraws_it(clean_db, paper_a):
    db = SessionLocal()
    try:
        project = services.create_project(db, "Fidelity")
        paper = models.Paper(project_id=project.id, title="Public sector pay",
                             pdf_status=models.PDF_DOWNLOADED, authors=[], issns=[],
                             source_ids={})
        db.add(paper)
        db.commit()
        source = pdfs.original_path(project.slug, paper.id)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(paper_a.read_bytes())
        assert paper_texts.ensure(paper.id) == paper_texts.READ
        text = db.get(models.PaperText, paper.id)
        db.refresh(text)
        assert text.page_labels == {str(n): str(70 + n) for n in range(1, 7)}
        findings = []
        for page, typed in QUOTES_A[:6]:
            found = highlight.match_quote(text.content, typed)
            findings.append({"title": "t", "quote": found["text"], "page": page,
                             "why": "w"})
        annotation = models.Annotation(paper_id=paper.id, question="q",
                                       status=models.ANNOT_DONE, findings=[])
        db.add(annotation)
        db.commit()
        results = highlight.highlight_annotation(project.slug, paper.id, annotation.id,
                                                 findings, content=text.content)
        results = highlight.enrich_from_text(results, text.content, paper_texts.page_at,
                                             paper_texts.section_at, text.page_labels)
        assert [r["located"] for r in results] == ["pdf"] * 6
        assert {r["printed_page"] for r in results} == {"71"}
        assert results[0]["section"] == "Abstract"
        annotation.findings = results
        db.commit()
        copy = pdfs.annotation_path(project.slug, annotation.id)
        with fitz.open(copy) as doc:
            drawn = sum(1 for _ in doc[0].annots())
        assert drawn == 6
        copy.unlink()
        rebuilt = highlight.rebuild_highlighted(annotation, paper)
        with fitz.open(rebuilt) as doc:
            assert sum(1 for _ in doc[0].annots()) == 6
    finally:
        db.close()


def test_highlights_carry_no_note_over_the_text(paper_a):
    """The rationale is not the annotation's contents: a viewer would draw a
    note icon over the passage's words for it ("& Gu[]pta")."""
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    with fitz.open(paper_a) as doc:
        highlight.locate_and_highlight(
            doc, [{"quote": QUOTES_A[3][1], "page": 1, "why": "A long rationale."}],
            content=content)
        annots = list(doc[0].annots())
        assert len(annots) == 1
        assert annots[0].info["content"] == ""
        assert annots[0].info["title"] == "LitReview"


def test_a_clicked_citation_is_singled_out(paper_a):
    from types import SimpleNamespace

    from litreview_local import citations

    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    paper = SimpleNamespace(id=4242, project=SimpleNamespace(slug="focus-test"))
    source = pdfs.original_path(paper.project.slug, paper.id)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(paper_a.read_bytes())
    with fitz.open(paper_a) as doc:
        findings = highlight.locate_and_highlight(
            doc, [{"quote": highlight.match_quote(content, typed)["text"], "page": page}
                  for page, typed in QUOTES_A[:3]], content=content)
    annotation = SimpleNamespace(id=77, findings=findings, updated_at=models.utcnow())
    key = citations.finding_key(annotation.id, findings[1])
    path = highlight.focused_copy(annotation, paper, key)
    assert path == highlight.rebuild_highlighted(annotation, paper, focus_key=key)
    assert path != pdfs.annotation_path(paper.project.slug, annotation.id)
    with fitz.open(path) as doc:
        colours = [tuple(round(c, 2) for c in annot.colors["stroke"])
                   for annot in doc[0].annots()]
    assert colours == [(1.0, 1.0, 0.0), highlight.FOCUS_COLOUR, (1.0, 1.0, 0.0)]
    assert highlight.focused_copy(annotation, paper, "77-00000000") is None
    # Drawn once; kept while the findings stay as they are.
    before = path.stat().st_mtime_ns
    annotation.updated_at = annotation.updated_at.replace(year=2001)
    assert highlight.focused_copy(annotation, paper, key) == path
    assert path.stat().st_mtime_ns == before


def test_a_refusal_shows_both_sides_as_they_are_written():
    content = ("<!-- page 1 -->\nRemote Work improved reported Wellbeing by twelve "
               "percent.\nEmployees slept LONGER on average.")
    quote = ("Remote work improved reported wellbeing by twelve percent. It should "
             "never be tried.")
    said = highlight.diagnose_miss(content, quote)
    agreed = len("Remote work improved reported wellbeing by twelve percent. ")
    assert said == (f"its first {agreed} of {len(quote)} characters match the text, "
                    "then it reads \"It should never be tried.\" where the text has "
                    "\"Employees slept LONGER on average.\"")
    assert highlight.diagnose_miss(content, "Employees slept longer on average. "
                                   "They woke rested.") \
        .endswith("then it reads \"They woke rested.\" where the text ends")


def test_a_tasks_page_tags_are_not_text():
    task_text = ("<page 1>\nFirst page words.\n</page 1>\n\n<page 2>\nThe effect of "
                 "the treat-\nment was large and significant in every year.\n</page 2>")
    near = highlight.closest_passage(task_text, "The effect of the treatment was "
                                     "large and significant in all years.")
    assert near is not None
    passage, page = near
    assert page == 2
    assert "<page" not in passage and "</page" not in passage
    assert paper_texts.page_at(task_text, task_text.index("treat-")) == 2
    assert highlight.match_quote(task_text, "First page words. The effect of the "
                                 "treatment")["text"] == ("First page words. The "
                                                          "effect of the treatment")


def test_a_hint_with_its_ligatures_resolved_is_still_highlighted(paper_a):
    """A refusal offers the closest passage with "fi" for the text's ligature;
    the passage then submitted is found and highlighted all the same."""
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    raw = paper_texts.compose([page for page in paper_texts.extract_pages(paper_a)])
    assert FF not in raw
    passage, page = highlight.closest_passage(
        content, "The estimated effect is statistically significant: the gap fell by "
                 "five per cent when pay was bargained centrally")
    assert page == 1 and "significant" in passage
    stored = highlight.match_quote(content, passage)["text"]
    with fitz.open(paper_a) as doc:
        for text in (content, None):
            result = highlight.locate_and_highlight(
                doc, [{"quote": stored, "page": page}], content=text)[0]
            assert result["matched"] and result["page"] == 1
        # What the highlighter used to do: PyMuPDF's default search keeps the
        # ligature, so the plain "ff" of the passage is not found.
        assert not doc[0].search_for("The estimated effect is statistically",
                                     flags=OLD_SEARCH_FLAGS)


def test_dates_formula_fragments_and_lowercase_lines_are_not_headings(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Remote Work and Wellbeing", fontsize=20)
    page.insert_text((72, 90), "June 7, 2025", fontsize=14)
    page.insert_text((72, 120), "transformation function))", fontsize=14)
    page.insert_text((72, 150), "methods", fontsize=10, fontname="hebo")
    page.insert_text((72, 180), "2 methods", fontsize=10, fontname="hebo")
    y = 210
    for _ in range(14):
        page.insert_text((72, y), "Body text about remote work and wellbeing, set in "
                         "the ordinary size of the paper.", fontsize=10)
        y += 14
    path = tmp_path / "junk.pdf"
    doc.save(str(path))
    doc.close()
    text = paper_texts.extract_pages(path)[0]
    assert "# Remote Work and Wellbeing\n" in text
    assert "\nJune 7, 2025\n" in text and "# June" not in text
    assert "\ntransformation function))\n" in text
    assert "\nmethods\n" in text
    assert "### 2 methods\n" in text


def _centred(page, y, text, size, font="helv"):
    width = fitz.get_text_length(text, fontname=font, fontsize=size)
    page.insert_text(((612 - width) / 2, y), text, fontsize=size, fontname=font)


def test_what_real_papers_set_large_or_bold_is_not_taken_for_headings(tmp_path):
    """The shapes met in real papers: a centred title over two lines, authors
    side by side, table header cells, a bold abstract, a reference entry
    set large, an R comment in a code listing, a rotated table's labels,
    and an appendix set larger than the paper."""
    doc = fitz.open()
    page = doc.new_page()
    _centred(page, 60, "The Gaussian Copula Control Function Method Does Not Help", 17)
    _centred(page, 82, "Against Traditional Omitted Variable Bias", 17)
    page.insert_text((150, 110), "Florian Dost", fontsize=12)
    page.insert_text((330, 110), "Rouven E. Haschka", fontsize=12)
    page.insert_text((72, 140), "Author(s)", fontsize=10, fontname="hebo")
    page.insert_text((300, 140), "Source of endogeneity mentioned", fontsize=10,
                     fontname="hebo")
    for n, line in enumerate(["Abstract. Knowledge management is a critical part of",
                              "the business intelligence and real-time decisions of",
                              "firms that compete on their data and their speed."]):
        page.insert_text((72, 170 + 13 * n), line, fontsize=10, fontname="hebo")
    page.insert_text((72, 230), "Danaher, P. J. (2023). Optimal microtargeting", fontsize=12)
    page.insert_text((72, 255), "# number of observations", fontsize=12, fontname="cour")
    page.insert_text((72, 270), "n <- 1000", fontsize=10, fontname="cour")
    page.insert_text((560, 400), "Rotated Table Header", fontsize=10, fontname="hebo",
                     rotate=90)
    y = 300
    for _ in range(20):
        page.insert_text((72, y), "Body text about remote work and wellbeing, set in "
                         "the ordinary size of the paper.", fontsize=10)
        y += 13
    appendix = doc.new_page()
    appendix.insert_text((72, 60), "Appendix A: Reviewed Articles", fontsize=12,
                         fontname="hebo")
    y = 90
    for n in range(24):
        appendix.insert_text((72, y), f"Article {n} in the review list, set in the "
                             "appendix's larger type", fontsize=12)
        y += 15
    path = tmp_path / "real-shapes.pdf"
    doc.save(str(path))
    doc.close()
    pages = paper_texts.extract_pages(path)
    content = paper_texts.compose(pages)
    headings = [h["heading"] for h in paper_texts_outline(content)]
    assert headings == ["The Gaussian Copula Control Function Method Does Not Help "
                        "Against Traditional Omitted Variable Bias",
                        "Appendix A: Reviewed Articles"]
    # Not headings - but all still in the text, the comment escaped so that
    # markdown does not take it for one.
    for kept in ("Florian Dost", "Author(s)", "Abstract. Knowledge management",
                 "Danaher, P. J. (2023)", "\\# number of observations",
                 "Rotated Table Header", "Article 3 in the review list"):
        assert kept in content
    # The escape is not the text's: quoted, shown and highlighted as printed.
    found = highlight.match_quote(content, "Optimal microtargeting # number of "
                                  "observations n <- 1000")
    assert found["text"] == "Optimal microtargeting # number of observations n <- 1000"
    with fitz.open(path) as doc:
        result = highlight.locate_and_highlight(
            doc, [{"quote": found["text"], "page": 1}], content=content)[0]
    assert result["matched"]


# ---------------------------------------------------------- printed pages

def test_page_labels_come_from_the_pdf_when_it_has_them(paper_b):
    labels = paper_texts.read_pdf(paper_b).page_labels
    assert labels == {"1": "233", "2": "234", "3": "235", "4": "236"}


def test_running_headers_give_the_printed_page(tmp_path):
    """A real case: JPA 62:71-90, where PDF page 8 is printed page 78.
    The first page prints no number; the others print it in the header, on
    alternating sides, next to the journal's name and the article's range."""
    pages = [layout(body("An article about public administration begins here."), FILLER,
                    footer=[(72, 760, f"JPA 62:71{DASH}90")])]
    for number in range(72, 82):
        header = ([(72, 40, str(number)), (200, 40, "JOURNAL OF PUBLIC ADMINISTRATION")]
                  if number % 2 == 0 else
                  [(72, 40, f"JPA 62:71{DASH}90 PUBLIC PAY"), (530, 40, str(number))])
        pages.append(layout(body(f"Page text on the page printed {number + 5} or so, "
                                 "with 2019 and 12 in it."), FILLER, header=header))
    path = build_pdf(tmp_path / "jpa.pdf", pages)
    labels = paper_texts.read_pdf(path).page_labels
    assert labels["8"] == "78"
    assert labels["1"] == "71"
    assert labels == {str(n): str(70 + n) for n in range(1, 12)}
    row = models.PaperText(paper_id=1, content="", reader="pymupdf", page_labels=labels)
    assert paper_texts.printed_page(row, 8) == "78"
    assert paper_texts.printed_page(row, "8") == "78"
    assert paper_texts.printed_page(row, 40) is None
    assert paper_texts.printed_page(models.PaperText(paper_id=1, content="",
                                                     reader="pymupdf"), 8) is None
    assert paper_texts.printed_page(None, 8) is None


def test_page_labels_that_only_count_the_pages_give_way_to_printed_numbers(tmp_path):
    pages = [layout(body("Text of a chapter."), FILLER, footer=[(300, 760, str(14 + n))])
             for n in range(1, 6)]
    path = build_pdf(tmp_path / "chapter.pdf", pages,
                     labels=[{"startpage": 0, "prefix": "", "style": "D",
                              "firstpagenum": 1}])
    assert paper_texts.read_pdf(path).page_labels == {str(n): str(14 + n)
                                                      for n in range(1, 6)}
    # Labels that count from 1 and nothing printed to contradict them stand.
    plain = [layout(body("Text of a preprint."), FILLER) for _ in range(3)]
    path = build_pdf(tmp_path / "preprint.pdf", plain,
                     labels=[{"startpage": 0, "prefix": "", "style": "D",
                              "firstpagenum": 1}])
    assert paper_texts.read_page_labels(path) == {"1": "1", "2": "2", "3": "3"}


def test_a_cover_page_and_no_numbers_at_all(tmp_path):
    cover = layout(body("This content downloaded from a library on a date."), FILLER)
    pages = [cover] + [layout(body("Article text."), FILLER,
                              footer=[(300, 760, str(100 + n))])
                       for n in range(1, 4)]
    labels = paper_texts.read_pdf(build_pdf(tmp_path / "jstor.pdf", pages)).page_labels
    assert labels["2"] == "101" and labels["4"] == "103"
    unnumbered = [layout(body(f"A working paper page {n} without any page numbers."),
                         FILLER) for n in range(4)]
    assert paper_texts.read_pdf(build_pdf(tmp_path / "wp.pdf", unnumbered)).page_labels == {}
    assert paper_texts.read_page_labels(tmp_path / "missing.pdf") is None


def test_enrich_from_text_adds_the_printed_page_and_keeps_its_old_arguments(paper_a):
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    labels = paper_texts.read_pdf(paper_a).page_labels
    finding = {"quote": highlight.match_quote(content, QUOTES_A[13][1])["text"],
               "page": 3, "matched": False, "rects": []}
    plain = highlight.enrich_from_text([finding], content, paper_texts.page_at,
                                       paper_texts.section_at)[0]
    assert plain["located"] == "text" and "printed_page" not in plain
    assert plain["section"] == "3 Results"
    labelled = highlight.enrich_from_text([finding], content, paper_texts.page_at,
                                          paper_texts.section_at, page_labels=labels)[0]
    assert labelled["printed_page"] == "73"


# ------------------------------------------------- re-reading a new text

def test_refresh_findings_rereads_against_claudes_parse(paper_a):
    content = paper_texts.compose(paper_texts.extract_pages(paper_a))
    labels = paper_texts.read_pdf(paper_a).page_labels
    _, placed = _placed(paper_a, [QUOTES_A[13], QUOTES_A[16]], with_text=True)
    highlighted = highlight.enrich_from_text([placed[0][2]], content, paper_texts.page_at,
                                             paper_texts.section_at)[0]
    in_text_only = dict(highlight.enrich_from_text(
        [dict(placed[1][2], matched=False, rects=[])], content, paper_texts.page_at,
        paper_texts.section_at)[0], page=1)
    gone = {"title": "t", "quote": "A passage only the old text had, long enough.",
            "page": 2, "located": "text", "section": "Old", "context": "old context",
            "matched": False, "rects": []}
    findings = [highlighted, in_text_only, gone]
    parsed = paper_texts.compose([
        "# Public Sector Pay and the Taste for Discrimination", "## Data",
        "## Main results\n\nTable 3 shows the main estimates. Centralised bargaining "
        "reduced the gender gap by 4.8 log points (s.e. 1.1), an effect that is "
        "robust to the inclusion of municipality-specific trends. The reduction is "
        "concentrated.\n\n## Design\n\nOur preferred estimate comes from a "
        "difference-in-differences design; it is 3.9 log points, with a confidence "
        "interval."])
    refreshed = highlight.refresh_findings(findings, parsed, labels)
    assert [f["quote"] for f in refreshed] == [f["quote"] for f in findings]
    assert [f["rects"] for f in refreshed] == [f["rects"] for f in findings]
    first, second, third = refreshed
    assert first["located"] == "pdf" and first["page"] == 3
    assert first["section"] == "Main results" and first["printed_page"] == "73"
    assert first["context"].endswith("The reduction is concentrated.")  # the new text's
    assert first["context"] != highlighted["context"]
    assert second["located"] == "text" and second["page"] == 3
    assert second["section"] == "Design" and second["printed_page"] == "73"
    assert third["located"] == "none" and third["page"] == 2
    assert third["section"] == "Old" and third["context"] == "old context"
    assert third["printed_page"] == "72"
    # Without labels, a printed page is kept unless the page moved.
    kept = highlight.refresh_findings([dict(first, printed_page="73")], parsed)
    assert kept[0]["printed_page"] == "73"
    moved = highlight.refresh_findings([dict(in_text_only, printed_page="71")], parsed)
    assert "printed_page" not in moved[0]


# ------------------------------------------------------------- the column

def test_an_older_library_gains_the_page_labels_column(clean_db):
    from sqlalchemy import inspect, text

    from litreview_local import db

    assert ("paper_texts", "page_labels", "JSON") == db._MIGRATION_COLUMNS[-1]
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE paper_texts DROP COLUMN page_labels"))
    assert "page_labels" not in {c["name"] for c in inspect(db.engine).get_columns(
        "paper_texts")}
    db.run_migrations()
    assert "page_labels" in {c["name"] for c in inspect(db.engine).get_columns(
        "paper_texts")}


def test_texts_kept_before_page_labels_get_them_at_startup(clean_db, paper_a):
    db = SessionLocal()
    try:
        project = services.create_project(db, "Older library")
        paper = models.Paper(project_id=project.id, title="Public sector pay",
                             pdf_status=models.PDF_DOWNLOADED, authors=[], issns=[],
                             source_ids={})
        other = models.Paper(project_id=project.id, title="Its PDF is gone",
                             pdf_status=models.PDF_DOWNLOADED, authors=[], issns=[],
                             source_ids={})
        db.add_all([paper, other])
        db.commit()
        source = pdfs.original_path(project.slug, paper.id)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(paper_a.read_bytes())
        db.add_all([models.PaperText(paper_id=paper.id, content="<!-- page 1 -->\nx",
                                     reader=models.READER_PYMUPDF),
                    models.PaperText(paper_id=other.id, content="<!-- page 1 -->\ny",
                                     reader=models.READER_PYMUPDF)])
        db.commit()
        assert paper_texts.fill_page_labels() == 1
        db.expire_all()
        assert paper_texts.printed_page(db.get(models.PaperText, paper.id), 8) is None
        assert paper_texts.printed_page(db.get(models.PaperText, paper.id), 4) == "74"
        assert db.get(models.PaperText, other.id).page_labels is None  # tried again later
        assert paper_texts.fill_page_labels() == 0
    finally:
        db.close()


def test_claudes_parse_keeps_the_page_labels(clean_db, paper_b):
    db = SessionLocal()
    try:
        project = services.create_project(db, "Parsed")
        paper = models.Paper(project_id=project.id, title="Minimum wages",
                             pdf_status=models.PDF_DOWNLOADED, authors=[], issns=[],
                             source_ids={})
        db.add(paper)
        db.commit()
        source = pdfs.original_path(project.slug, paper.id)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(paper_b.read_bytes())
        row = paper_texts.store(db, paper, ["# Minimum wages\n\nText."] * 4,
                                models.READER_CLAUDE)
        assert row.page_labels == {"1": "233", "2": "234", "3": "235", "4": "236"}
        assert paper_texts.printed_page(row, 3) == "235"
    finally:
        db.close()
