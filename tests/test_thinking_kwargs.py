"""Explicit think-block policy for the Wisp-merged Ling models.

Background (2026-10-13): Ling-3.0-tiny-Wisp-V3-merged-oQ4e was fine-tuned
thinking-off, emits no reasoning under its default template, and only thinks
when `enable_thinking=True` is sent. Wisp used to send `False` or nothing, so
its "thinking" paths inherited the oMLX admin profile's toggle. These tests pin
the explicit policy and, just as importantly, that no other model's kwargs
changed.
"""
from __future__ import annotations

import inspect

import pytest

import service.config as config
from service.agent import loop

V3 = "Ling-3.0-tiny-Wisp-V3-merged-oQ4e"
V2 = "Ling-3.0-tiny-Wisp-V2-merged-oQ4e"
OLD_LING = "Ling-3.0-tiny-oQ4e"          # no_thinking_capable, not explicit
A1 = "Agents-A1-4B-oQe6"                 # no_thinking_capable, not explicit
OTHER = "some-other-model"               # neither

ON = {"chat_template_kwargs": {"enable_thinking": True}}
OFF = {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    monkeypatch.setattr(config, "explicit_thinking_models", lambda: [V2, V3])
    monkeypatch.setattr(config, "no_thinking_capable", lambda: [A1, OLD_LING])


def test_packaged_roster_lists_the_merged_models():
    cfg = config.models_config()
    assert set(cfg.get("explicit_thinking", [])) >= {V2, V3, "Ling-3.0-tiny-Wisp-V3-merged"}


def test_explicit_models_state_both_directions():
    assert config.thinking_kwargs(V3, on=True) == ON
    assert config.thinking_kwargs(V3, on=False) == OFF
    assert config.no_thinking_kwargs(V3) == OFF


def test_other_models_are_byte_identical_to_before():
    # The False form only for models verified to honor it; "on" stays an omission.
    assert config.no_thinking_kwargs(A1) == OFF
    assert config.no_thinking_kwargs(OLD_LING) == OFF
    assert config.no_thinking_kwargs(OTHER) == {}
    assert config.thinking_kwargs(A1, on=True) == {}
    assert config.thinking_kwargs(OLD_LING, on=True) == {}
    assert config.thinking_kwargs(OTHER, on=True) == {}
    assert config.thinking_kwargs(OTHER, on=False) == {}


def test_user_facing_summaries_think_explicitly_on_v3_only():
    assert config.user_facing_summary_kwargs(V3) == ON
    assert config.user_facing_summary_kwargs(OLD_LING) == {}       # unchanged exemption
    assert config.user_facing_summary_kwargs(A1) == OFF            # unchanged optimization


def test_summary_budget_grows_only_with_an_explicit_think_block():
    assert config.user_facing_summary_budget(V3, 512) == 1800
    assert config.user_facing_summary_budget(V3, 2500) == 2500     # never shrinks
    assert config.user_facing_summary_budget(OLD_LING, 512) == 512
    assert config.user_facing_summary_budget(A1, 600) == 600


def test_loop_step_kwargs_follow_the_policy():
    # Explicit model: narration/selection distinction is irrelevant, think_on decides.
    assert loop._template_kwargs(V3, no_thinking=True, think_on=False) == OFF
    assert loop._template_kwargs(V3, no_thinking=False, think_on=False) == OFF
    assert loop._template_kwargs(V3, no_thinking=False, think_on=True) == ON
    # Other models: exactly the old behavior, think_on ignored.
    assert loop._template_kwargs(A1, no_thinking=True) == OFF
    assert loop._template_kwargs(A1, no_thinking=False) == {}
    assert loop._template_kwargs(A1, no_thinking=False, think_on=True) == {}
    assert loop._template_kwargs(OTHER, no_thinking=True) == {}


def test_loop_turns_thinking_on_only_after_an_outstanding_failure():
    src = inspect.getsource(loop.run_agent)
    assert "step_think_on = bool(failed_tools - tools_answered)" in src
    assert "think_on=step_think_on" in src
    assert "experiment(" not in src
