"""Deterministic source-quality classification.

"Trusted domain = true" is explicitly not a claim this module makes. It only
proposes a class and a human-readable reason; the report renders both as
metadata the user can weigh, never as ground truth.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_ACADEMIC_DOMAINS = {"arxiv.org", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
                     "scholar.google.com", "jstor.org", "nature.com", "science.org",
                     "acm.org", "ieee.org", "springer.com", "sciencedirect.com",
                     "plos.org", "biorxiv.org", "ssrn.com"}
_ACADEMIC_TLDS = (".edu", ".ac.uk", ".ac.jp", ".ac.nz", ".ac.in")
_OFFICIAL_TLDS = (".gov", ".mil")
_OFFICIAL_SUFFIXES = ("docs.", "developer.", "developers.", "support.", "help.",
                     "status.", "blog.", "engineering.")
_COMMUNITY_DOMAINS = {"reddit.com", "quora.com", "medium.com", "substack.com",
                      "pinterest.com", "tiktok.com", "facebook.com", "x.com",
                      "twitter.com", "stackoverflow.com", "stackexchange.com",
                      "news.ycombinator.com", "answers.yahoo.com"}
_THIN_MARKERS = ("copied from", "reproduced with permission", "originally published on",
                 "this article first appeared", "syndicated from")


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
    return "reputable_secondary", f"{domain} appears to be an independent publisher with original content"


def quality_bonus(quality_class: str) -> float:
    """A small ranking nudge, not a truth claim — primary/official/academic
    sources get a modest boost over an unclassified or community source."""
    return {"academic": 0.15, "official": 0.15, "primary": 0.2,
            "reputable_secondary": 0.05, "community": -0.1}.get(quality_class, 0.0)
