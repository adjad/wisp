"""Experiment switches (service/config/experiments.py)."""
from __future__ import annotations

import inspect

import pytest

from service.config import experiments


def test_unset_means_nothing_active():
    assert experiments.parse(None) == frozenset()
    assert experiments.parse("") == frozenset()
    assert experiments.parse(" , ") == frozenset()


def test_parse_accepts_known_names_case_and_space_insensitively():
    assert experiments.parse(" Capture_Think ") == {"capture_think"}


def test_unknown_name_fails_loudly():
    with pytest.raises(ValueError, match="Unknown WISP_EXPERIMENTS"):
        experiments.parse("capture_think,captrue_think")


def test_retired_switches_are_rejected_not_silently_ignored():
    """A retired name must fail at startup, or an old eval invocation would
    quietly measure the baseline under the experiment's label."""
    for retired in ("search_think", "narration_think_compound"):
        with pytest.raises(ValueError):
            experiments.parse(retired)


def test_experiment_default_off_and_unknown_query_raises(monkeypatch):
    monkeypatch.setattr(experiments, "_ACTIVE", frozenset())
    for name in experiments.KNOWN:
        assert experiments.experiment(name) is False
    with pytest.raises(ValueError):
        experiments.experiment("not_a_switch")


def test_experiment_reads_active_set(monkeypatch):
    monkeypatch.setattr(experiments, "_ACTIVE", frozenset({"capture_think"}))
    assert experiments.experiment("capture_think") is True
    assert experiments.active() == {"capture_think"}


def test_every_known_switch_is_wired_somewhere():
    """A switch nobody reads is a leftover; a reader of a deleted switch raises
    at call time, so the sweep goes the other way: each KNOWN name must appear
    in the module that owns it."""
    from service.memory import capture
    src = inspect.getsource(capture)
    assert "experiment('capture_think')" in src
    assert experiments.KNOWN == {"capture_think"}
