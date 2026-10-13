"""verify(): an uncited sentence is kept when one retrieved passage accounts for
its content words, and cited to that passage.

Measured 2026-10-13 on Ling-3.0-tiny-Wisp-V3-merged-oQ4e: 6/6 Smart Search
answers correct, 3/6 carrying a [n], the other three dropped as "uncited" —
a right answer shown as "not found" half the time. The provenance guarantee
stays: nothing is kept that cannot be traced to a passage.
"""
from __future__ import annotations

from service.search.chunker import Chunk
from service.search.synth import NOT_FOUND, verify

P1 = ("Flour costs rose 14 percent after the March supplier change; the team "
      "switched from Harvest Mills to Prairie Grain on March 18.")
P2 = ("Open risk: the lease on the Elm Avenue kitchen expires on September 30 "
      "and the landlord has not confirmed renewal terms.")
P3 = "Next week: inhibitor screen at 37 C using lot E-2291 only, triplicates."
CHUNKS = [Chunk(idx=i, text=t, start=0, end=len(t)) for i, t in enumerate([P1, P2, P3])]
PICKS = [0, 1, 2]


def test_model_citation_is_untouched():
    text, used = verify("They switched to Prairie Grain on March 18 [1].", CHUNKS, PICKS)
    assert text == "They switched to Prairie Grain on March 18 [1]."
    assert used == [0]


def test_uncited_paraphrase_is_grounded_and_cited():
    text, used = verify(
        "The open risk is that the lease on the Elm Avenue kitchen expires on "
        "September 30 and the landlord has not confirmed renewal terms.", CHUNKS, PICKS)
    assert text.endswith("renewal terms [2].")
    assert used == [1]


def test_short_exact_answer_is_grounded_only_on_a_full_match():
    text, used = verify("37 C.", CHUNKS, PICKS)
    assert text == "37 C [3]."
    assert used == [2]
    # A short answer with a token the passages don't contain is still dropped.
    assert verify("42 C.", CHUNKS, PICKS) == (NOT_FOUND, [])


def test_sentence_about_something_else_is_still_dropped():
    assert verify("The bakery opened a new branch in Boston last year.", CHUNKS, PICKS) == (NOT_FOUND, [])


def test_mixed_answer_keeps_grounded_sentences_and_drops_the_rest():
    text, used = verify(
        "Lot E-2291 is used for the inhibitor screen at 37 C. "
        "The CEO announced record profits.", CHUNKS, PICKS)
    # verify() cites by passage number; _answer renumbers to first-use order later.
    assert "inhibitor screen at 37 C [3]." in text
    assert "record profits" not in text
    assert used == [2]


def test_global_scope_behavior_is_unchanged():
    # Global scope already kept uncited sentences; grounding must not start
    # adding citations there.
    text, used = verify("The document is a lab notebook about enzyme assays.",
                        CHUNKS, PICKS, scope="global")
    assert "[" not in text
    assert used == []
