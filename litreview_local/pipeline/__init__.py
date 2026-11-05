from . import crossref, dedupe, normalize, openalex, semantic_scholar, unpaywall  # noqa: F401

SOURCES = {
    "openalex": openalex,
    "semantic_scholar": semantic_scholar,
    "crossref": crossref,
}

SOURCE_LABELS = {
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "crossref": "Crossref",
}
