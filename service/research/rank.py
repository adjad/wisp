"""Deterministic source-quality classification.

"Trusted domain = true" is explicitly not a claim this module makes. It only
proposes a class and a human-readable reason; the report renders both as
metadata the user can weigh, never as ground truth.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from service.search.lexical import stem, tokenize

_ACADEMIC_DOMAINS = {"arxiv.org", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
                     "scholar.google.com", "jstor.org", "nature.com", "science.org",
                     "acm.org", "ieee.org", "springer.com", "sciencedirect.com",
                     "plos.org", "biorxiv.org", "ssrn.com", "openalex.org"}
_ACADEMIC_TLDS = (".edu", ".ac.uk", ".ac.jp", ".ac.nz", ".ac.in")
_OFFICIAL_TLDS = (".gov", ".mil")
_OFFICIAL_SUFFIXES = ("docs.", "developer.", "developers.", "support.", "help.",
                     "status.", "blog.", "engineering.")
_COMMUNITY_DOMAINS = {"reddit.com", "quora.com", "medium.com", "substack.com",
                      "pinterest.com", "tiktok.com", "facebook.com", "x.com",
                      "twitter.com", "stackoverflow.com", "stackexchange.com",
                      "news.ycombinator.com", "answers.yahoo.com", "wikipedia.org",
                      "wikimedia.org", "wiktionary.org"}
_THIN_MARKERS = ("copied from", "reproduced with permission", "originally published on",
                 "this article first appeared", "syndicated from")

# Words that describe the act or shape of research rather than its subject.
# Counting these as topical anchors is how a page about an athletic *field*
# survived a query about vaccine field performance. Keep this deliberately
# small: the hard requirement below is multiple independent matches, so this
# list does not need to understand every possible research domain.
_GENERIC_RESEARCH_TERMS = {
    "analysis", "compare", "comparison", "effect", "efficacy", "evidence",
    "field", "finding", "findings", "form", "impact", "influence", "long",
    "mechanism", "mechanisms", "performance", "potency", "recent", "report",
    "research", "result", "results", "review", "state", "study", "versus",
}


def classify_source(*, url: str, domain: str, title: str, text: str = "") -> tuple[str, str]:
    """Return (class, reason). class is one of primary, official, academic,
    reputable_secondary, community, or unknown."""
    domain = (domain or urlparse(url).hostname or "").lower().removeprefix("www.")
    host_parts = domain.split(".")
    root = ".".join(host_parts[-2:]) if len(host_parts) >= 2 else domain

    if domain in _ACADEMIC_DOMAINS or root in _ACADEMIC_DOMAINS or domain.endswith(_ACADEMIC_TLDS):
        return "academic", f"{domain} is a recognized academic/research publisher or .edu-class domain"
    if domain.endswith(_OFFICIAL_TLDS):
        return "official", f"{domain} is a government/military domain"
    if any(domain.startswith(prefix) for prefix in _OFFICIAL_SUFFIXES):
        return "official", f"{domain} looks like an official documentation or engineering subdomain"
    if domain in _COMMUNITY_DOMAINS or root in _COMMUNITY_DOMAINS:
        return "community", f"{domain} is a user-generated/community platform, not an editorial source"

    lowered = (text or "")[:600].lower()
    if any(marker in lowered for marker in _THIN_MARKERS):
        return "community", "page text indicates syndicated/reproduced content, not the original publisher"

    word_count = len(re.findall(r"[a-zA-Z]{3,}", title or ""))
    if not domain or word_count == 0:
        return "unknown", "insufficient metadata to classify this source"
    # A syntactically valid publisher is not evidence of an editorial process.
    # The old default painted cannabis shops, dictionaries, and arbitrary small
    # sites as "reputable secondary". Unknown is the only honest default; known
    # classes above can still receive an explicit quality label.
    return "unknown", f"{domain} has not been independently classified"


def quality_bonus(quality_class: str) -> float:
    """A small ranking nudge, not a truth claim — primary/official/academic
    sources get a modest boost over an unclassified or community source."""
    return {"academic": 0.15, "official": 0.15, "primary": 0.2,
            "reputable_secondary": 0.05, "community": -0.1}.get(quality_class, 0.0)


def _topic_terms(text: str) -> set[str]:
    out = set()
    for word in tokenize(text):
        if len(word) < 4 or word in _GENERIC_RESEARCH_TERMS:
            continue
        root = stem(word)
        # The intentionally tiny stemmer used by local document search keeps
        # offsets simple, but research relevance only compares sets and can
        # safely normalize a few high-frequency derivational families.
        if root in {"freez", "freeze", "frozen"}:
            root = "freeze"
        elif root.startswith("vaccin"):
            root = "vaccine"
        elif root in {"stor", "store", "stored", "storage"}:
            root = "storage"
        elif root.startswith("lyophiliz") or root.startswith("lyophilis"):
            root = "lyophiliz"
        elif root.startswith("structur"):
            root = "structure"
        elif root.startswith("stabil"):
            root = "stability"
        out.add(root)
    return out


def relevance_score(*, text: str, objective: str, query: str) -> tuple[float, dict[str, int]]:
    """Return a conservative lexical topicality score and its audit counts.

    Search-engine rank is not relevance. In particular, a single broad token
    must not admit a dictionary definition or a page from an unrelated sense
    of the word. A result needs evidence from both the research objective and
    the specific query, or several specific query matches.
    """
    found = _topic_terms(text)
    objective_terms = _topic_terms(objective)
    query_terms = _topic_terms(query)
    objective_hits = len(found & objective_terms)
    query_hits = len(found & query_terms)
    distinct_hits = len(found & (objective_terms | query_terms))

    objective_den = max(1, min(4, len(objective_terms)))
    query_den = max(1, min(5, len(query_terms)))
    score = 0.45 * min(1.0, objective_hits / objective_den)
    score += 0.55 * min(1.0, query_hits / query_den)
    # A useful result normally connects the broad topic to the narrower search.
    # Three query-specific terms can stand alone for a narrowly titled paper.
    relevant = ((objective_hits >= 1 and query_hits >= 1 and distinct_hits >= 2)
                or query_hits >= 3)
    if not relevant:
        score = 0.0
    return score, {"objective_hits": objective_hits, "query_hits": query_hits,
                   "distinct_hits": distinct_hits}
