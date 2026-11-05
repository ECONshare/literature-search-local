"""SQLAlchemy ORM models for LitReview Local.

The same data model as the server app - projects, research questions,
searches, papers, annotations, documents - with the multi-user layer taken out
(one person owns everything on their own machine) and two things added:

- ClaudeTask: a step only a model can do, waiting in a queue until the
  user's own Claude picks it up through the MCP server. See tasks.py.
- Tag: free-form labels on articles, per project.
"""
import re
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import event, func, select
from sqlalchemy.orm import DeclarativeBase, Session, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(stamp: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; every one stored here is UTC."""
    if stamp is not None and stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


class Base(DeclarativeBase):
    pass


# Screening statuses (stage 1: mark papers for the next stage)
STATUS_UNSCREENED = "unscreened"
STATUS_INCLUDED = "included"
STATUS_EXCLUDED = "excluded"
STATUS_MAYBE = "maybe"
STATUSES = [STATUS_UNSCREENED, STATUS_INCLUDED, STATUS_EXCLUDED, STATUS_MAYBE]

# Workflow stages
STAGE_SCREENING = "screening"  # just searched/ranked; being screened
STAGE_RETRIEVAL = "retrieval"  # selected for reading; PDF being fetched
# A hit from "add a known article" that nobody has picked yet. Kept out of
# the article list and the counts, so a lookup cannot fill the project with
# near-misses; picking it moves it to screening, and unpicked ones are
# cleared away after a week (services.prune_lookups).
STAGE_LOOKUP = "lookup"
STAGES = [STAGE_SCREENING, STAGE_RETRIEVAL]

# PDF statuses (within the retrieval stage)
PDF_NONE = "none"
PDF_PENDING = "pending"
PDF_DOWNLOADED = "downloaded"
PDF_FAILED = "failed"
PDF_STATUSES = [PDF_NONE, PDF_PENDING, PDF_DOWNLOADED, PDF_FAILED]

# Which reader produced a text. PyMuPDF reads the PDF's own text layer, on the
# spot and for free; Claude reads it when asked (scanned papers, tables,
# equations), through a parse_pdf task.
READER_PYMUPDF = "pymupdf"
READER_CLAUDE = "claude"
READER_LABELS = {READER_PYMUPDF: "Text layer", READER_CLAUDE: "Parsed by Claude"}

ANNOT_PENDING = "pending"   # a task for it is waiting for Claude
ANNOT_DONE = "done"
ANNOT_FAILED = "failed"
ANNOT_NEEDS_OCR = "needs_ocr"  # no text layer: waiting for Claude to parse it

# Project document (meta-PDF) extraction statuses
DOC_PENDING = "pending"
DOC_DONE = "done"
DOC_FAILED = "failed"

# Research-question pipeline statuses. Four of them are Claude's turn - a task
# is waiting in the queue - and two are the app's own work in the background.
RQ_QUEUED = "queued"              # the app is about to start the next step
RQ_PARSING = "parsing"            # Claude: turn the question into a search
RQ_SEARCHING = "searching"        # app: querying the sources
RQ_RANKING = "ranking"            # Claude: score the results
RQ_DOWNLOADING = "downloading"    # app: fetching the PDFs
RQ_ANNOTATING = "annotating"      # Claude: read the papers
RQ_SYNTHESIZING = "synthesizing"  # Claude: write the answer
RQ_DONE = "done"
RQ_FAILED = "failed"
RQ_RUNNING_STATUSES = [RQ_QUEUED, RQ_PARSING, RQ_SEARCHING, RQ_RANKING,
                       RQ_DOWNLOADING, RQ_ANNOTATING, RQ_SYNTHESIZING]
# The statuses in which nothing moves until Claude has done its part.
RQ_CLAUDE_STATUSES = [RQ_PARSING, RQ_RANKING, RQ_ANNOTATING, RQ_SYNTHESIZING]
# The statuses in which the app itself is working in the background, and which
# are picked up again at startup if the app was closed in the middle of them.
RQ_APP_STATUSES = [RQ_QUEUED, RQ_SEARCHING, RQ_DOWNLOADING]

RQ_STATUS_LABELS = {
    RQ_QUEUED: "Starting",
    RQ_PARSING: "Waiting for Claude to plan the search",
    RQ_SEARCHING: "Searching the sources",
    RQ_RANKING: "Waiting for Claude to rank the results",
    RQ_DOWNLOADING: "Downloading PDFs",
    RQ_ANNOTATING: "Waiting for Claude to read the papers",
    RQ_SYNTHESIZING: "Waiting for Claude to write the answer",
    RQ_DONE: "Done",
    RQ_FAILED: "Failed",
}


def slugify(name: str) -> str:
    """Filesystem-safe slug used for the project's PDF folder."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    slug = Column(String(200), nullable=False, unique=True)  # PDF folder name
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    # Pipeline parameters used when a research question runs. Smaller than
    # the server app's: every article ranked and every paper read is work
    # done inside the user's own Claude subscription.
    fetch_per_source = Column(Integer, nullable=False, default=25)   # X
    rank_top = Column(Integer, nullable=False, default=40)           # Y
    download_top = Column(Integer, nullable=False, default=6)        # Z
    sources = Column(Text, nullable=False,
                     default="openalex,semantic_scholar,crossref")

    papers = relationship("Paper", back_populates="project",
                          cascade="all, delete-orphan")
    searches = relationship("Search", back_populates="project",
                            cascade="all, delete-orphan")
    questions = relationship("ResearchQuestion", back_populates="project",
                             cascade="all, delete-orphan",
                             order_by="ResearchQuestion.created_at.desc()")
    documents = relationship("ProjectDocument", back_populates="project",
                             cascade="all, delete-orphan",
                             order_by="ProjectDocument.created_at")
    tags = relationship("Tag", back_populates="project",
                        cascade="all, delete-orphan", order_by="Tag.name")
    tasks = relationship("ClaudeTask", back_populates="project",
                         cascade="all, delete-orphan", passive_deletes=True)

    def source_list(self) -> list[str]:
        return [s for s in (self.sources or "").split(",") if s]


class ProjectDocument(Base):
    """A meta-PDF attached to a project.

    Not a search hit: the documents a review leans on rather than reviews -
    a protocol, a coding manual, the funder's guidelines, a prior review, a
    data dictionary, the manuscript being written. Read with PyMuPDF on
    upload; Claude can parse it properly (tables, equations, scans) on
    request. The extracted text lives in the row, because every consumer
    wants the text and not the file.
    """
    __tablename__ = "project_documents"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    title = Column(Text, nullable=False)
    # What this document is for, in the user's words. Carried to the MCP tool
    # listing so an agent can tell a coding manual from a funder's template
    # without reading either.
    description = Column(Text, nullable=True)
    filename = Column(Text, nullable=True)  # as uploaded
    status = Column(String(20), nullable=False, default=DOC_PENDING)
    error = Column(Text, nullable=True)
    reader = Column(String(30), nullable=True)  # READER_PYMUPDF / READER_CLAUDE
    content = Column(Text, nullable=True)  # markdown (LaTeX for equations)
    page_count = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)

    project = relationship("Project", back_populates="documents")


class ResearchQuestion(Base):
    """A plain-text research question driving one pipeline run.

    The question owns its searches and annotations, and carries the
    synthesized, user-editable answer (HTML with <cite> spans that reference
    annotation findings, see citations.py).
    """
    __tablename__ = "research_questions"
    # Two questions in one project must never answer to the same name.
    __table_args__ = (
        Index("uq_rq_project_number", "project_id", "number", unique=True,
              sqlite_where=text("number IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    text = Column(Text, nullable=False)
    # Position within its project, counting from 1 in the order the questions
    # were asked. Assigned once and never reused: a draft citing RQ2 must go
    # on meaning this question after RQ1 is deleted, so the numbers carry
    # gaps rather than closing them.
    number = Column(Integer, nullable=True)
    # A few words naming the question, written by the same step that plans
    # the search.
    headline = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, default=RQ_QUEUED, index=True)
    status_detail = Column(Text, nullable=True)  # human-readable progress line
    error = Column(Text, nullable=True)
    # {"keywords", "areas", "query", "concept_groups", "boolean", "year_from",
    #  "year_to"} from the parse step
    parsed = Column(JSON, nullable=False, default=dict)
    # Where the current run has got to, so a run interrupted by closing the
    # app picks up from the step it was on: {"mode", "search_id", ...}.
    run_state = Column(JSON, nullable=False, default=dict)
    # Parameter snapshot for the latest run (copied from the project).
    fetch_per_source = Column(Integer, nullable=True)
    rank_top = Column(Integer, nullable=True)
    download_top = Column(Integer, nullable=True)
    summary_html = Column(Text, nullable=True)  # the editable answer
    # When the answer was last written from the evidence - the clock that
    # says whether an annotation is already reflected in the prose.
    answer_written_at = Column(DateTime(timezone=True), nullable=True)
    # Claude's check of the answer's citations against their passages
    # (a verify_answer task): {"digest": the answer it checked, "checked_at",
    # "verdicts": {key: {"verdict", "note"}}, "overall"}. Only shown while the
    # digest matches the answer on the page.
    verification = Column(JSON, nullable=False, default=dict)
    # Where the run was started: "web" or "mcp".
    created_by = Column(String(20), nullable=True)
    mcp_session = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)

    project = relationship("Project", back_populates="questions")
    searches = relationship("Search", back_populates="research_question")
    annotations = relationship("Annotation", back_populates="research_question",
                               order_by="Annotation.id")

    @property
    def label(self) -> str:
        """What to call this question in prose: RQ1, RQ2, ..."""
        return f"RQ{self.number}" if self.number else f"#{self.id}"

    @property
    def running(self) -> bool:
        return self.status in RQ_RUNNING_STATUSES

    @property
    def waiting_for_claude(self) -> bool:
        return self.status in RQ_CLAUDE_STATUSES

    @property
    def status_label(self) -> str:
        return RQ_STATUS_LABELS.get(self.status, self.status)


@event.listens_for(Session, "before_flush")
def _number_new_questions(session, flush_context, instances) -> None:
    """Give every new question the next number in its project.

    Per flush rather than per insert, because SQLAlchemy batches inserts of
    the same class into one statement: numbering them one at a time meant two
    questions added together both read the same highest number.
    """
    pending = [obj for obj in session.new
               if isinstance(obj, ResearchQuestion)
               and obj.number is None and obj.project_id]
    if not pending:
        return
    highest: dict[int, int] = {}
    for rq in pending:
        if rq.project_id not in highest:
            highest[rq.project_id] = session.execute(
                select(func.max(ResearchQuestion.number))
                .where(ResearchQuestion.project_id == rq.project_id)
            ).scalar() or 0
        highest[rq.project_id] += 1
        rq.number = highest[rq.project_id]


search_papers = Table(
    "search_papers",
    Base.metadata,
    Column("search_id", ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True),
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
)

paper_tags = Table(
    "paper_tags",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Search(Base):
    __tablename__ = "searches"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=True, index=True)
    research_question_id = Column(
        Integer, ForeignKey("research_questions.id", ondelete="SET NULL"),
        nullable=True, index=True)
    raw_query = Column(Text, nullable=True)  # the user's free-text request
    query = Column(Text, nullable=False)  # the actual search query
    sources = Column(Text, nullable=False, default="")  # comma-separated (+ error notes)
    year_from = Column(Integer, nullable=True)
    year_to = Column(Integer, nullable=True)
    result_count = Column(Integer, nullable=False, default=0)  # total fetched
    new_count = Column(Integer, nullable=False, default=0)  # new after dedupe
    ranked = Column(Integer, nullable=False, default=0)  # 1 once ranking ran
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    project = relationship("Project", back_populates="searches")
    research_question = relationship("ResearchQuestion", back_populates="searches")
    papers = relationship("Paper", secondary=search_papers, back_populates="searches")
    scores = relationship("PaperScore", back_populates="search",
                          cascade="all, delete-orphan")


class PaperScore(Base):
    """One paper's relevance score against one search's goal.

    Scores are per search (and thus per research question) - a paper hit by
    two questions is scored once against each. Paper.llm_score/llm_reason
    mirror the most recent score purely for display.
    """
    __tablename__ = "paper_scores"
    __table_args__ = (UniqueConstraint("search_id", "paper_id",
                                       name="uq_paper_scores_search_paper"),)

    id = Column(Integer, primary_key=True)
    search_id = Column(Integer, ForeignKey("searches.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    score = Column(Float, nullable=False)  # 0-100
    reason = Column(Text, nullable=True)

    search = relationship("Search", back_populates="scores")
    paper = relationship("Paper")


class Paper(Base):
    __tablename__ = "papers"
    # The same DOI may appear once per project, but not twice within one.
    __table_args__ = (UniqueConstraint("project_id", "doi", name="uq_papers_project_doi"),)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=True, index=True)
    doi = Column(String(255), nullable=True, index=True)
    title = Column(Text, nullable=False)
    abstract = Column(Text, nullable=True)
    # The version of record's year - the issue's, which is what gets cited -
    # and the earliest year it appeared, online first. They differ when an
    # article went online a year or more before its issue (normalize.py).
    year = Column(Integer, nullable=True, index=True)
    online_year = Column(Integer, nullable=True)
    venue = Column(Text, nullable=True)
    # Where in the journal: "50", "1", and "46-66", a first page alone, or an
    # article number.
    volume = Column(String(40), nullable=True)
    issue = Column(String(40), nullable=True)
    pages = Column(String(40), nullable=True)
    # Journal identity, for looking up quality ratings.
    issn_l = Column(String(9), nullable=True, index=True)
    issns = Column(JSON, nullable=False, default=list)
    # article / book / preprint / report / book-chapter / ...
    work_type = Column(String(40), nullable=True)
    authors = Column(JSON, nullable=False, default=list)  # list of names
    citation_count = Column(Integer, nullable=True)
    url = Column(Text, nullable=True)  # landing page
    oa_url = Column(Text, nullable=True)  # open-access PDF / page if known
    source_ids = Column(JSON, nullable=False, default=dict)  # {"openalex": "...", ...}
    dedupe_key = Column(String(500), nullable=True, index=True)  # title+year fallback key

    status = Column(String(20), nullable=False, default=STATUS_UNSCREENED, index=True)
    note = Column(Text, nullable=True)

    # Relevance ranking (stage 2)
    llm_score = Column(Float, nullable=True, index=True)  # 0-100
    llm_reason = Column(Text, nullable=True)

    # Workflow stage + PDF retrieval (stages 3-4)
    stage = Column(String(20), nullable=False, default=STAGE_SCREENING, index=True)
    pdf_status = Column(String(20), nullable=False, default=PDF_NONE)
    pdf_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    project = relationship("Project", back_populates="papers")
    searches = relationship("Search", secondary=search_papers, back_populates="papers")
    annotations = relationship("Annotation", back_populates="paper",
                               cascade="all, delete-orphan",
                               order_by="Annotation.created_at.desc()")
    tags = relationship("Tag", secondary=paper_tags, back_populates="papers",
                        order_by="Tag.name")

    @property
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]


class Tag(Base):
    """A free-form label on articles - "method: DiD", "to read", "Chapter 2".

    Per project, because a label means something within one review. Matched
    case-insensitively through `key`, so "To read" and "to read" are one tag,
    shown the way it was first typed.
    """
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_tags_project_key"),)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name = Column(String(100), nullable=False)
    key = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    project = relationship("Project", back_populates="tags")
    papers = relationship("Paper", secondary=paper_tags, back_populates="tags")


def tag_key(name: str) -> str:
    """The case- and spacing-insensitive identity of a tag name."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def clean_tag_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:100]


class PaperText(Base):
    """The full text of one paper's PDF, kept once the PDF has been read.

    Every page opens with a <!-- page N --> marker, N being the PDF's own page
    number - the numbering highlights carry. A table of its own rather than a
    column on papers, because every list of papers loads whole rows and a long
    paper's text runs to a hundred thousand characters.
    """
    __tablename__ = "paper_texts"

    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"),
                      primary_key=True)
    content = Column(Text, nullable=False)
    reader = Column(String(30), nullable=False)  # READER_PYMUPDF / READER_CLAUDE
    page_count = Column(Integer, nullable=False, default=0)
    char_count = Column(Integer, nullable=False, default=0)
    # The number printed on each PDF page, {"8": "78", ...} keyed by PDF page
    # (paper_texts.detect_page_labels): what a citation needs, where the
    # <!-- page N --> markers count the PDF's pages. {} = worked out, none
    # found; NULL = not worked out yet (paper_texts.fill_page_labels).
    page_labels = Column(JSON(none_as_null=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)


# Where an article added from its PDF is in finding out what it is (see
# identify.py). CONFIRM waits for the person; CLASSIFYING, PICKING and WEB
# wait for Claude; QUEUED, PARSING and SEARCHING are the app's own work.
IDENT_QUEUED = "queued"
IDENT_PARSING = "parsing"
IDENT_CLASSIFYING = "classifying"  # Claude: what is this document?
IDENT_SEARCHING = "searching"      # app: the scholarly sources
IDENT_PICKING = "picking"          # Claude: which record is it?
IDENT_CONFIRM = "confirm"          # the user: "Is this it?"
IDENT_WEB = "web"                  # Claude: one web page about it
IDENT_DONE = "done"
IDENT_FAILED = "failed"
IDENT_RUNNING = [IDENT_QUEUED, IDENT_PARSING, IDENT_CLASSIFYING,
                 IDENT_SEARCHING, IDENT_PICKING, IDENT_WEB]
IDENT_CLAUDE = [IDENT_CLASSIFYING, IDENT_PICKING, IDENT_WEB]

# Where its details came from once it is done.
IDENTIFIED_MATCHED = "matched"     # a record in OpenAlex, Semantic Scholar or Crossref
IDENTIFIED_WEB = "web"             # the PDF itself, and one web page about it
IDENTIFIED_NONE = "none"           # the PDF itself: no external source


class PaperIdentification(Base):
    """How an article added from its PDF found out what it is.

    An uploaded PDF arrives as an article with nothing but a file name. It is
    parsed, classified as scholarly or not, looked up in the scholarly
    sources and, failing that, on the web - and every step says what it
    decided in `log`, so the card can show why it ended where it did.
    """
    __tablename__ = "paper_identifications"

    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"),
                      primary_key=True)
    filename = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=IDENT_QUEUED)
    outcome = Column(String(20), nullable=True)   # IDENTIFIED_*
    # The matched record's source (openalex / semantic_scholar / crossref).
    source = Column(String(30), nullable=True)
    scholarly = Column(Boolean, nullable=True)
    page_count = Column(Integer, nullable=True)
    # What the PDF says about itself - title, authors, year, ... - as the
    # classification read it. The details an unmatched article keeps.
    described = Column(JSON, nullable=False, default=dict)
    # Identifiers found on the PDF and the records the sources returned,
    # carried between the steps that the queue splits apart.
    found = Column(JSON, nullable=False, default=dict)
    candidates = Column(JSON, nullable=False, default=list)
    # The record waiting for "Is this it?", with the reason it was not taken
    # without asking.
    candidate = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    # [{"step": ..., "said": ...}, ...] in the order the steps ran.
    log = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)

    paper = relationship("Paper")

    @property
    def running(self) -> bool:
        return self.status in IDENT_RUNNING

    @property
    def waiting_for_claude(self) -> bool:
        return self.status in IDENT_CLAUDE


class Journal(Base):
    """Quality signals for one journal, keyed by a single ISSN."""
    __tablename__ = "journals"

    id = Column(Integer, primary_key=True)
    issn = Column(String(9), nullable=False, unique=True, index=True)
    title = Column(Text, nullable=True)
    # Norwegian Register (Kanalregisteret): 2 = top ~20% of the field,
    # 1 = approved, 0 = listed but not approved. NULL = not in the register.
    norwegian_level = Column(Integer, nullable=True)
    npi_field = Column(Text, nullable=True)
    # OpenAlex backup signals.
    openalex_id = Column(String(60), nullable=True)
    two_yr_citedness = Column(Float, nullable=True)
    h_index = Column(Integer, nullable=True)
    is_core = Column(Boolean, nullable=True)  # CWTS Leiden core list
    # Set once OpenAlex has been asked, so a journal it does not know is not
    # re-queried on every paper.
    openalex_checked = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)


class Annotation(Base):
    """What Claude found in one paper for one research question (stage 5)."""
    __tablename__ = "annotations"

    id = Column(Integer, primary_key=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    research_question_id = Column(
        Integer, ForeignKey("research_questions.id", ondelete="CASCADE"),
        nullable=True, index=True)
    question = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default=ANNOT_PENDING)
    error = Column(Text, nullable=True)
    # list of {"title", "quote", "page", "why", "matched": bool, "rects": [...]}
    findings = Column(JSON, nullable=False, default=list)
    model = Column(String(100), nullable=True)
    # Reader of the text Claude was given (READER_PYMUPDF / READER_CLAUDE).
    reader = Column(String(30), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Moves when the findings are rewritten: it is what tells the answer
    # apart from the evidence it was written from.
    updated_at = Column(DateTime(timezone=True), nullable=True, default=utcnow,
                        onupdate=utcnow)

    paper = relationship("Paper", back_populates="annotations")
    research_question = relationship("ResearchQuestion", back_populates="annotations")


# ------------------------------------------------------------ Claude's queue

TASK_PARSE_QUESTION = "parse_question"
TASK_RANK_PAPERS = "rank_papers"
TASK_ANNOTATE_PAPER = "annotate_paper"
TASK_SYNTHESIZE = "synthesize"
TASK_REVISE_ANSWER = "revise_answer"
TASK_ASSESS_ARTICLE = "assess_article"
TASK_IDENTIFY_PDF = "identify_pdf"
TASK_MATCH_RECORD = "match_record"
TASK_FIND_WEB_PAGE = "find_web_page"
TASK_PARSE_PDF = "parse_pdf"
TASK_VERIFY_ANSWER = "verify_answer"
TASK_KINDS = [TASK_PARSE_QUESTION, TASK_RANK_PAPERS, TASK_ANNOTATE_PAPER,
              TASK_SYNTHESIZE, TASK_REVISE_ANSWER, TASK_ASSESS_ARTICLE,
              TASK_IDENTIFY_PDF, TASK_MATCH_RECORD, TASK_FIND_WEB_PAGE,
              TASK_PARSE_PDF, TASK_VERIFY_ANSWER]

TASK_OPEN = "open"            # waiting for Claude
TASK_CLAIMED = "claimed"      # Claude is on it (until the lease runs out)
TASK_DONE = "done"
TASK_CANCELLED = "cancelled"  # no longer needed (question edited, deleted...)
TASK_FAILED = "failed"        # given up on after too many bad answers
TASK_LIVE_STATUSES = [TASK_OPEN, TASK_CLAIMED]


class ClaudeTask(Base):
    """One step only a model can do, waiting for the user's own Claude.

    The app never calls a model. Wherever the server app called Azure OpenAI
    or Document Intelligence, this app writes a task instead; Claude, working
    in Claude Desktop or Claude Code on the user's subscription, takes tasks
    with next_task and answers them with submit_task (mcp_server.py). Each
    task renders its own instructions and output schema (tasks.py), so Claude
    needs no prior knowledge of the procedure.

    A claimed task carries a lease. A chat that closes mid-task simply lets it
    run out, and the task goes back to the queue - nothing is lost, because
    nothing is applied until a valid answer is submitted.
    """
    __tablename__ = "claude_tasks"

    id = Column(Integer, primary_key=True)
    kind = Column(String(30), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=TASK_OPEN, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    research_question_id = Column(
        Integer, ForeignKey("research_questions.id", ondelete="CASCADE"),
        nullable=True, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id", ondelete="CASCADE"),
                      nullable=True, index=True)
    document_id = Column(Integer,
                         ForeignKey("project_documents.id", ondelete="CASCADE"),
                         nullable=True, index=True)
    annotation_id = Column(Integer,
                           ForeignKey("annotations.id", ondelete="CASCADE"),
                           nullable=True, index=True)
    # Lower runs first: a question waiting on its last step should not sit
    # behind a dozen PDFs being identified.
    priority = Column(Integer, nullable=False, default=100)
    # What this particular task is about, beyond the rows it points at: the
    # paper ids of a ranking batch, the page range of an annotation chunk,
    # the author's instruction for a revision.
    payload = Column(JSON, nullable=False, default=dict)
    # What Claude submitted - kept so a result can be inspected afterwards.
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)  # last refusal, or why it failed
    attempts = Column(Integer, nullable=False, default=0)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    claimed_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="tasks")
    research_question = relationship("ResearchQuestion")
    paper = relationship("Paper")
    document = relationship("ProjectDocument")
    annotation = relationship("Annotation")

    @property
    def live(self) -> bool:
        return self.status in TASK_LIVE_STATUSES


class AppState(Base):
    """Small facts the app keeps about itself: when Claude last called in,
    which client it was. Key/value, so a new fact needs no migration."""
    __tablename__ = "app_state"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        onupdate=utcnow)
