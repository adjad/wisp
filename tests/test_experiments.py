"""Experiment switches (service/config/experiments.py) and the one gate they
reach into — the narration-step predicate in run_agent.

The predicate is re-stated here the same way tests/test_narration_thinking.py
does it: kept in lockstep with the loop by construction, so a change to the
real condition's shape is what updates these tests.
"""
from __future__ import annotations

import pytest

from service.config import experiments


def test_unset_means_nothing_active():
    assert experiments.parse(None) == frozenset()
    assert experiments.parse("") == frozenset()
    assert experiments.parse(" , ") == frozenset()


def test_parse_accepts_known_names_case_and_space_insensitively():
    got = experiments.parse(" Search_Think , narration_think_compound ")
    assert got == {"search_think", "narration_think_compound"}


def test_unknown_name_fails_loudly():
    with pytest.raises(ValueError, match="Unknown WISP_EXPERIMENTS"):
        experiments.parse("search_think,serach_think")


def test_experiment_default_off_and_unknown_query_raises(monkeypatch):
    monkeypatch.setattr(experiments, "_ACTIVE", frozenset())
    for name in experiments.KNOWN:
        assert experiments.experiment(name) is False
    with pytest.raises(ValueError):
        experiments.experiment("not_a_switch")


def test_experiment_reads_active_set(monkeypatch):
    monkeypatch.setattr(experiments, "_ACTIVE", frozenset({"capture_think"}))
    assert experiments.experiment("capture_think") is True
    assert experiments.experiment("search_think") is False
    assert experiments.active() == {"capture_think"}


# ---------------------------------------------------------------- narration gate

def _narrating(*, answered: set[str], compound_experiment: bool, mode: str = "broad",
               tools=("a", "b"), multi_round=False, hard_failed=False, failed=frozenset()):
    """run_agent's `narrating` predicate with the experiment term appended."""
    return (
        mode != "off"
        and tools is not None
        and not multi_round
        and not hard_failed
        and not (set(failed) - answered)
        and bool(answered)
        and mode == "broad"
        and not (compound_experiment and len(answered) >= 2)
    )


def test_compound_experiment_keeps_thinking_only_for_two_or_more_results():
    one = {"get_upcoming"}
    two = {"summarize_emails", "summarize_messages"}
    # Off: today's behavior — narration skips the think block either way.
    assert _narrating(answered=one, compound_experiment=False)
    assert _narrating(answered=two, compound_experiment=False)
    # On: a single read-out still skips thinking (the measured 6x win stays);
    # a cross-source synthesis keeps it.
    assert _narrating(answered=one, compound_experiment=True)
    assert not _narrating(answered=two, compound_experiment=True)


def test_compound_experiment_never_widens_the_gate():
    """The experiment can only ADD a reason to keep thinking — every existing
    reason (error, multi_round, mode off) still keeps it on its own."""
    two = {"a", "b"}
    assert not _narrating(answered=two, compound_experiment=False, mode="off")
    assert not _narrating(answered=two, compound_experiment=False, multi_round=True)
    assert not _narrating(answered=two, compound_experiment=False, hard_failed=True)
    assert not _narrating(answered=two, compound_experiment=False, failed={"c"})


def test_loop_wires_the_switch(monkeypatch):
    """The real loop consults experiment(); a stale import would make the
    switch a no-op without any test noticing."""
    import inspect
    from service.agent import loop
    src = inspect.getsource(loop.run_agent)
    assert 'experiment("narration_think_compound")' in src
    assert "len(tools_answered) >= 2" in src
