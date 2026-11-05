"""Export helpers: CSV, BibTeX, RIS. Pure functions over paper dicts.

The dicts come from paper_row. BibTeX and RIS name every author "Surname,
Given", split the way the APA reference splits them (citations.split_name),
so a reference manager files "van der Berg" with its particle and a
hyphenated given name stays one person. Each entry's type follows the
paper's work_type, and the version of record's year, volume, issue and pages
go where Zotero, EndNote and Word's importers look for them.
"""
import csv
import io
import re
import unicodedata

from .citations import is_organization, split_name


def paper_row(paper) -> dict:
    """The dict the exporters take, from a Paper row."""
    return {"title": paper.title, "authors": paper.authors or [],
            "year": paper.year, "online_year": getattr(paper, "online_year", None),
            "venue": paper.venue, "volume": getattr(paper, "volume", None),
            "issue": getattr(paper, "issue", None),
            "pages": getattr(paper, "pages", None),
            "work_type": getattr(paper, "work_type", None), "doi": paper.doi,
            "url": paper.url, "oa_url": paper.oa_url,
            "citation_count": paper.citation_count, "status": paper.status,
            "note": paper.note, "abstract": paper.abstract,
            "tags": [t.name for t in getattr(paper, "tags", [])]}


def _line(value) -> str:
    """A value on one line: RIS is line-based, and so is a CSV cell read by eye."""
    return " ".join(str(value).split()) if value is not None else ""


# ---------------------------------------------------------------------- CSV

CSV_FIELDS = ["title", "authors", "year", "online_year", "venue", "volume",
              "issue", "pages", "doi", "url", "oa_url", "citation_count",
              "status", "note", "abstract"]


def _rows(papers: list[dict]) -> list[dict]:
    rows = []
    for p in papers:
        row = {field: p.get(field) if p.get(field) is not None else ""
               for field in CSV_FIELDS}
        row["authors"] = "; ".join(p.get("authors") or [])
        rows.append(row)
    return rows


def to_csv(papers: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in _rows(papers):
        writer.writerow(row)
    return buf.getvalue()


# ------------------------------------------------------------------- BibTeX

# work_type -> entry type. A paper whose type nobody reported is most likely
# a journal article; any other type not listed is @misc.
_BIBTEX_TYPES = {
    None: "article", "article": "article", "review": "article",
    "letter": "article", "editorial": "article", "erratum": "article",
    "book": "book", "monograph": "book",
    "book-chapter": "incollection", "conference-paper": "inproceedings",
    "report": "techreport", "working-paper": "techreport",
    "policy-brief": "techreport", "dissertation": "phdthesis",
    "thesis": "phdthesis",
}
# Where each entry type puts the venue.
_BIBTEX_CONTAINER = {"article": "journal", "incollection": "booktitle",
                     "inproceedings": "booktitle", "book": "publisher",
                     "techreport": "institution", "phdthesis": "school",
                     "misc": "howpublished"}
_REPORT_LABELS = {"working-paper": "Working Paper", "policy-brief": "Policy Brief"}

# Letters NFKD does not take apart, for the ASCII of a cite key.
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe",
                       "Œ": "OE", "ß": "ss", "ł": "l", "Ł": "L", "đ": "d",
                       "Đ": "D", "þ": "th", "Þ": "Th", "ı": "i"})
_KEY_SKIP = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "with",
             "from", "at", "by", "is", "are", "do", "does", "how", "what",
             "why", "when", "who", "which", "can"}


def _ascii_word(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_FOLD))
    return re.sub(r"[^a-z0-9]", "", text.encode("ascii", "ignore").decode().lower())


def _cite_key(p: dict, seen: set[str]) -> str:
    """surname + year + first word of the title - becker2022revisiting - so a
    paper keeps its key from one export to the next, whatever else is in it.
    Only papers that agree on all three are told apart, by a letter."""
    authors = [a for a in p.get("authors") or [] if (a or "").strip()]
    surname = split_name(authors[0])[0] if authors else ""
    words = (_ascii_word(w) for w in re.split(r"[\s/:\-]+", p.get("title") or ""))
    word = next((w for w in words if w and w not in _KEY_SKIP), "")
    base = f"{_ascii_word(surname) or 'anon'}{p.get('year') or ''}{word}"
    key, i = base, 1
    while key in seen:
        i += 1
        key = f"{base}{chr(ord('a') + i - 1)}"
    seen.add(key)
    return key


def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def _bibtex_escape(value: str) -> str:
    """A text field's value: braces escaped, LaTeX's specials (& % $ # _)
    escaped, everything else - ü, ø, é - kept as it is. A brace without its
    partner would end the field early, so then every brace is spelled out."""
    value = _line(value)
    braces = ({"{": "\\{", "}": "\\}"} if _balanced(value) else
              {"{": "\\textbraceleft{}", "}": "\\textbraceright{}"})
    return re.sub(r"[{}&%$#_]",
                  lambda m: braces.get(m.group(), "\\" + m.group()), value)


def _bibtex_verbatim(value: str) -> str:
    """A DOI or URL, which styles print as given: only a brace (it would end
    the field) is percent-encoded."""
    return _line(value).replace("{", "%7B").replace("}", "%7D")


def _bibtex_name(name: str) -> str:
    """Surname, Given (Surname, Jr., Given with a suffix); an organization
    in braces, so BibTeX does not take its last word for a surname."""
    surname, given, suffix = split_name(name)
    if not surname:
        return ""
    if not given and not suffix:
        return ("{" + _bibtex_escape(surname) + "}" if " " in surname
                else _bibtex_escape(surname))
    return ", ".join(_bibtex_escape(part) for part in (surname, suffix, given) if part)


def to_bibtex(papers: list[dict]) -> str:
    seen: set[str] = set()
    entries = []
    for p in papers:
        work_type = p.get("work_type")
        entry_type = _BIBTEX_TYPES.get(work_type, "misc")
        names = [n for n in (_bibtex_name(a) for a in p.get("authors") or []) if n]
        fields = [  # (field, value as it goes between the braces)
            ("author", " and ".join(names)),  # each name escaped already
            ("title", _bibtex_escape(p.get("title"))),
            (_BIBTEX_CONTAINER.get(entry_type, "howpublished"),
             _bibtex_escape(p.get("venue"))),
            ("type", _bibtex_escape(_REPORT_LABELS.get(work_type))),
            ("year", _line(p.get("year"))),
            ("volume", _bibtex_escape(p.get("volume"))),
            ("number", _bibtex_escape(p.get("issue"))),
            ("pages", re.sub(r"-+", "--", _bibtex_escape(p.get("pages")))),
            ("doi", _bibtex_verbatim(p.get("doi"))),
            ("url", _bibtex_verbatim(p.get("url"))),
        ]
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
        entries.append(f"@{entry_type}{{{_cite_key(p, seen)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


# ---------------------------------------------------------------------- RIS

# work_type -> TY. Unknown types are generic (GEN); no type at all is
# taken for a journal article.
_RIS_TYPES = {
    None: "JOUR", "article": "JOUR", "review": "JOUR", "letter": "JOUR",
    "editorial": "JOUR", "erratum": "JOUR", "book": "BOOK", "monograph": "BOOK",
    "book-chapter": "CHAP", "conference-paper": "CPAPER", "report": "RPRT",
    "working-paper": "RPRT", "policy-brief": "RPRT", "preprint": "UNPB",
    "dissertation": "THES", "thesis": "THES", "web-page": "ELEC",
    "news-article": "NEWS",
}


def _ris_name(name: str) -> str:
    """Surname, Given[, Suffix] - RIS's form. An organization gets a
    trailing comma, which is how EndNote tells a body from a person (Zotero
    reads it as a surname with no given name)."""
    surname, given, suffix = split_name(name)
    if not surname:
        return ""
    if not given and not suffix:
        return surname + "," if is_organization(surname) else surname
    return ", ".join(part for part in (surname, given, suffix) if part)


def to_ris(papers: list[dict]) -> str:
    records: list[str] = []
    for p in papers:
        ty = _RIS_TYPES.get(p.get("work_type"), "GEN")
        lines = [f"TY  - {ty}"]

        def add(tag: str, value) -> None:
            value = _line(value)
            if value:
                lines.append(f"{tag}  - {value}")

        add("TI", p.get("title"))
        for author in p.get("authors") or []:
            add("AU", _ris_name(author))
        add("PY", p.get("year"))
        venue = p.get("venue")
        if ty == "JOUR":
            add("JO", venue)
            add("T2", venue)
        elif ty in ("CHAP", "CPAPER", "NEWS"):
            add("T2", venue)  # the book, proceedings or newspaper
        else:
            add("PB", venue)  # publisher, institution, university, archive
        add("VL", p.get("volume"))
        add("IS", p.get("issue"))
        first, _, last = _line(p.get("pages")).partition("-")
        add("SP", first)
        add("EP", last)
        add("DO", p.get("doi"))
        add("UR", p.get("url"))
        add("AB", p.get("abstract"))
        for tag in p.get("tags") or []:
            add("KW", tag)
        lines.append("ER  - ")
        records.append("\n".join(lines))
    return "\n\n".join(records) + ("\n" if records else "")
