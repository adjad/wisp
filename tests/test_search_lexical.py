"""Smart Search's T1 lexical tier: folding, stemming, and BM25 ranking.

All pure functions over strings — no oMLX, no embeddings, no network.
"""
from __future__ import annotations

import pytest

from service.search.chunker import Chunk
from service.search.lexical import BM25, fold, literal_hits, query_terms, stem, tokenize


# --------------------------------------------------------------------------
# fold: lowercase + strip diacritics, WITHOUT moving any character offsets
# --------------------------------------------------------------------------

def test_fold_lowercases():
    assert fold("Hello World") == "hello world"


def test_fold_strips_diacritics():
    assert fold("café") == "cafe"


@pytest.mark.parametrize("text", ["café", "naïve résumé", "Ünicode", "plain ascii"])
def test_fold_preserves_length(text):
    """The whole point of folding this way rather than via NFD: highlight
    offsets computed on folded text have to stay valid against the original.
    """
    assert len(fold(text)) == len(text)


# --------------------------------------------------------------------------
# stem: crude, but query and document must agree
# --------------------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("running", "run"),
    ("carries", "carry"),
    ("carried", "carry"),
    ("walked", "walk"),
    ("boxes", "box"),
    ("cats", "cat"),
    ("quickly", "quick"),
])
def test_stem_known_forms(word, expected):
    assert stem(word) == expected


@pytest.mark.parametrize("word", ["is", "as", "his", "gas", "bus"])
def test_stem_leaves_short_words_alone(word):
    """The length guard exists so 'is' doesn't stem to 'i'."""
    assert stem(word) == word


def test_stem_is_idempotent_enough_to_match():
    """Query and document go through the same function, so what matters is that
    the two sides land on the same token, not that it's a real word."""
    assert stem("running") == stem("running")


# --------------------------------------------------------------------------
# tokenize
# --------------------------------------------------------------------------

def test_tokenize_splits_and_folds():
    assert tokenize("The Quick, Brown Fox!") == ["the", "quick", "brown", "fox"]


def test_tokenize_empty():
    assert tokenize("") == []


# --------------------------------------------------------------------------
# literal_hits — the T0 tier, exact substring matches
# --------------------------------------------------------------------------

def test_literal_hits_finds_a_match():
    hits = literal_hits("the cat sat on the mat", "cat")
    assert len(hits) == 1


def test_literal_hits_is_case_insensitive():
    assert literal_hits("The CAT sat", "cat")


def test_literal_hits_absent_query():
    assert literal_hits("the cat sat", "elephant") == []


def test_literal_hits_respects_limit():
    text = "cat " * 500
    assert len(literal_hits(text, "cat", limit=10)) == 10


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------

def _chunks(*texts: str) -> list[Chunk]:
    return [Chunk(idx=i, text=t, start=0, end=len(t)) for i, t in enumerate(texts)]


def test_bm25_ranks_the_relevant_chunk_first():
    chunks = _chunks(
        "The mitochondria is the powerhouse of the cell.",
        "Bananas are a popular yellow fruit grown in the tropics.",
        "Cell membranes regulate what enters and leaves a cell.",
    )
    bm = BM25(chunks)
    ranked = bm.search("mitochondria powerhouse")
    assert ranked
    assert ranked[0].chunk_idx == 0


def test_bm25_returns_nothing_for_absent_terms():
    bm = BM25(_chunks("apples and oranges", "pears and plums"))
    assert bm.search("quantum chromodynamics") == []


def test_bm25_matches_across_inflection():
    """A search for 'running' should find 'run' — that's what stemming buys."""
    bm = BM25(_chunks("he was running late", "completely unrelated text"))
    ranked = bm.search("run")
    assert ranked and ranked[0].chunk_idx == 0


def test_bm25_empty_index():
    assert BM25([]).search("anything") == []


def test_bm25_scores_descend():
    chunks = _chunks(
        "cat cat cat cat",
        "cat and a dog",
        "nothing relevant here at all",
    )
    ranked = BM25(chunks).search("cat")
    scores = [h.score for h in ranked]
    assert scores == sorted(scores, reverse=True)


def test_query_terms_drops_stopwords():
    terms = query_terms("what is the powerhouse of the cell")
    assert "the" not in terms
    assert "of" not in terms
