from __future__ import annotations

import unittest

import service.tools  # noqa: F401
from service.router.reranker import _action_clauses, lexical_candidates, lexical_rank


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

