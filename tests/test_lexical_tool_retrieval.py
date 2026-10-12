from __future__ import annotations

import unittest
from unittest.mock import Mock

import pytest

import service.tools  # noqa: F401
from service.router import reranker
from service.router.reranker import _action_clauses, lexical_candidates, lexical_rank
from service.router.semantic import _PINNED
from service.tools.registry import REGISTRY, Tool
from scripts.bench_routing_overhead import CASES, uncached_shortlist


class LexicalToolRetrievalTests(unittest.TestCase):
    def test_explicit_file_crypto_action_survives_without_reranker(self) -> None:
        query = ("decrypt /tmp/wisp-routing-fixtures/keep/encrypted-test.txt.enc "
                 "using dummy-file-password")
        self.assertEqual("encrypt_file", lexical_rank(query)[0])
        self.assertIn("encrypt_file", lexical_candidates(query, writing=True, k=20))

    def test_explicit_task_list_is_split_without_splitting_noun_lists(self) -> None:
        query = ("Please do these in this order: check my calendar tomorrow; "
                 "then text Mom the result; then save the summary to a file.")
        self.assertEqual([
            "check my calendar tomorrow",
            "text Mom the result",
            "save the summary to a file.",
        ], _action_clauses(query))

    def test_compound_shortlist_contains_each_action(self) -> None:
        query = ("Check my calendar tomorrow. Text Mom the result. "
                 "Save the summary to a file.")
        offered = set(lexical_candidates(query, writing=True, k=20))
        self.assertTrue({"get_upcoming", "send_message", "write_file"} <= offered)


# Rank reuse preserves every vote, tool boundary and next-request update.
def test_single_clause_ranks_once_without_losing_depth_votes(monkeypatch):
    names = [f"tool_{i:02}" for i in range(40)]
    ranked = Mock(return_value=names)
    monkeypatch.setattr(reranker, "lexical_rank", ranked)
    assert reranker.lexical_shortlist("show synthetic fixture", limit=40) == names[:30]
    ranked.assert_called_once_with("show synthetic fixture")


def test_repeated_clause_retains_votes_and_distinct_queries(monkeypatch):
    rankings = {"full request": ["file", "music"],
                "calendar clause": ["calendar", "file"],
                "music clause": ["music", "file"]}
    ranked = Mock(side_effect=rankings.__getitem__)
    monkeypatch.setattr(reranker, "lexical_rank", ranked)
    monkeypatch.setattr(reranker, "_action_clauses", lambda _: ["calendar clause", "calendar clause", "music clause"])
    # Calendar wins over music only if both repeated-clause votes survive.
    assert reranker.lexical_shortlist("full request") == ["file", "calendar", "music"]
    assert [c.args[0] for c in ranked.call_args_list] == ["full request", "calendar clause", "music clause"]


def test_exact_query_keys_are_not_normalized(monkeypatch):
    ranked = Mock(side_effect=lambda q: [q])
    monkeypatch.setattr(reranker, "lexical_rank", ranked)
    monkeypatch.setattr(reranker, "_action_clauses", lambda _: ["Open Safari", "open Safari", "Open Safari "])
    reranker.lexical_shortlist("Open Safari")
    assert [c.args[0] for c in ranked.call_args_list] == ["Open Safari", "open Safari", "Open Safari "]


@pytest.mark.parametrize("limit", [0, 1, 15, 30, 60])
def test_tie_order_and_limit_match_uncached_fusion(monkeypatch, limit):
    monkeypatch.setattr(reranker, "_action_clauses", lambda _: ["clause a", "clause b", "clause a"])
    rankings = {"whole": ["zeta", "alpha"], "clause a": ["beta", "alpha"], "clause b": ["alpha", "beta"]}
    monkeypatch.setattr(reranker, "lexical_rank", rankings.__getitem__)
    assert reranker.lexical_shortlist("whole", limit=limit) == uncached_shortlist(reranker, "whole", limit=limit)


def test_real_registry_aliases_and_adversarial_menus_match_reference(monkeypatch):
    candidate = reranker.lexical_shortlist
    index = reranker._lexical_index()
    prompts = [p for _, p, _ in CASES]
    prompts.extend(REGISTRY[n].aliases[0] for n in index.names if REGISTRY[n].aliases)
    for i, prompt in enumerate(prompts):
        limit = [1, 5, 15, 20, 60][i % 5]
        assert candidate(prompt, limit=limit) == uncached_shortlist(reranker, prompt, limit=limit), prompt
        if i < len(CASES):
            for writing in (False, True):
                actual = reranker.lexical_candidates(prompt, writing=writing)
                with monkeypatch.context() as scoped:
                    scoped.setattr(reranker, "lexical_shortlist", lambda text, *, limit: uncached_shortlist(reranker, text, limit=limit))
                    expected = reranker.lexical_candidates(prompt, writing=writing)
                assert actual == expected, (prompt, writing)
                assert set(_PINNED) <= set(actual)


def test_registry_and_in_place_alias_changes_are_fresh_between_requests(monkeypatch):
    monkeypatch.setattr(reranker, "_LEXICAL", None)
    def inert(**kwargs):
        raise AssertionError("Tool execution is outside a retrieval test")
    tool = Tool("fixture_reuse", "neutral fixture", {}, "fs_read", inert, aliases=["quasarneedle"])
    monkeypatch.setitem(REGISTRY, tool.name, tool)
    assert tool.name in reranker.lexical_shortlist("quasarneedle")
    tool.aliases[:] = ["differentneedle"]
    assert tool.name not in reranker.lexical_shortlist("quasarneedle")
    assert tool.name in reranker.lexical_shortlist("differentneedle")
    tool.unavailable_reason = "synthetic unavailable"
    assert tool.name not in reranker.lexical_shortlist("differentneedle")


def test_silent_file_mutation_gate_and_pins_survive_reuse(monkeypatch):
    monkeypatch.setattr(reranker, "_LEXICAL", None)
    def inert(**kwargs):
        raise AssertionError("Retrieval must not execute a tool")
    tool = Tool("fixture_delete", "quasarneedle", {}, "fs_delete", inert)
    monkeypatch.setitem(REGISTRY, tool.name, tool)
    readonly = reranker.lexical_candidates("quasarneedle", writing=False)
    writing = reranker.lexical_candidates("quasarneedle", writing=True)
    assert tool.name not in readonly and tool.name in writing
    assert set(_PINNED) <= set(readonly) & set(writing)


@pytest.mark.parametrize("query", ["Uninstall Cobalt app", "Please uninstall Cobalt app",
    "Can you uninstall Cobalt app", "Could you uninstall Cobalt app", "Would you uninstall Cobalt app"])
def test_explicit_uninstall_survives_normal_legacy_retrieval(query):
    assert "uninstall_app" in lexical_candidates(query, writing=False, k=20)


@pytest.mark.parametrize("query", ["Do not uninstall Cobalt app", "Don't uninstall Cobalt app",
    "How do I uninstall Cobalt app?", "Explain uninstall", "Show my calendar"])
def test_uninstall_visibility_fix_does_not_open_on_negation_or_explanation(query):
    from service.router.semantic import _gate_open
    assert not _gate_open(query, writing=False)
    assert "uninstall_app" not in lexical_candidates(query, writing=False, k=20)


def test_typo_neighbours_do_not_depend_on_vocab_iteration_order():
    from service.search.chunker import Chunk
    from service.search.lexical import BM25

    docs = ["meeting at 8am", "gym class at 7 am", "change the contact name",
            "rename the nam field", "sam called about the ham"]
    bm25 = BM25([Chunk(i, d, 0, len(d)) for i, d in enumerate(docs)])
    expected = [(h.chunk_idx, h.score) for h in bm25.search("gym 7am")]

    class Reordered(set):
        def __iter__(self):
            return iter(sorted(set.__iter__(self), reverse=True))

    bm25.vocab = Reordered(bm25.vocab)
    assert [(h.chunk_idx, h.score) for h in bm25.search("gym 7am")] == expected


def test_typo_recovery_is_stable_across_hash_seeds():
    # "7am" is not in the tool vocabulary; its within-one-edit neighbours used
    # to be taken in set order, so seeds 12/13/35 picked "nam" and dropped
    # add_calendar_event from the menu (routing-quality calendar_write-006).
    import os
    import subprocess
    import sys

    probe = ("import json, service.tools\n"
             "from service.router.reranker import lexical_candidates\n"
             "print(json.dumps(lexical_candidates('can you add gym to my cal tmrw 7am', writing=True)))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    menus = []
    for seed in ("0", "12", "13", "35"):
        proc = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True, text=True,
                              timeout=120, env={**os.environ, "PYTHONHASHSEED": seed})
        assert proc.returncode == 0, proc.stderr[-2000:]
        menus.append(proc.stdout.strip().splitlines()[-1])
    assert len(set(menus)) == 1, menus
    assert "add_calendar_event" in menus[0]
