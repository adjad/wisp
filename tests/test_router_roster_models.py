"""The router never names a model id that is absent from the configured roster.

Regression for 2026-10-13. router.py pinned every web-lookup route (news,
weather, stock and crypto prices, "search the web for ...") to a literal
``"Ling-3.0-tiny-oQ6e"``. That id had been dropped from oMLX, so
``POST /v1/models/Ling-3.0-tiny-oQ6e/load`` returned 404, the engine then
unloaded and reloaded the resident model for a "runtime settings variant",
and the next two /agent turns failed with
"The local AI engine returned an error (409)".

Three properties keep that from recurring:

  * router.py contains no string literal that looks like a model id;
  * with ``role_to_model`` replaced by a sentinel, every route still emits the
    sentinel, so no route reaches a model by any path other than the roster;
  * with the real configuration, every routed model is one the roster names.

    .venv/bin/python -m pytest -q tests/test_router_roster_models.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_scratch = tempfile.TemporaryDirectory()
os.environ["WISP_HOME"] = _scratch.name

from service.config import models_config, role_to_model  # noqa: E402
from service.router.router import route  # noqa: E402

ROUTER_PATH = Path(__file__).resolve().parents[1] / "service" / "router" / "router.py"

# Every model family that has ever been rostered or served by oMLX here. A
# literal matching any of these inside router.py is a model id by another name.
_MODEL_ID_RE = re.compile(
    r"Ling-|Qwen|LFM|Ornith|gpt-oss|Agents-A1|TurboFieldfare|Bonsai|"
    r"-oQ\d|-MLX-|\d+bit\b|-DWQ|-nvfp4"
)

# One prompt per route family that used to carry the literal, plus ordinary
# routes, so the sentinel check covers the whole decision surface and not just
# the web paths.
_PROMPTS = (
    "what's happening in Iran",
    "what is the situation in Ukraine right now",
    "search the web for the latest Python release",
    "look up the latest Python release online",
    "what is the price of bitcoin",
    "check the stock prices for my stocks",
    "what's the weather in Austin today",
    "latest news about Iran; do not browse; save it in Notes",
    "what's happening in Sudan? Use only what you already know.",
    "what is happening in Iran and text Mom a summary",
    "look up the latest Python release online; email it to our team inbox",
    "search the web for the Zorvian treaty and save it to my notes",
    "when is the next Apple event",
    "what's on my calendar tomorrow",
    "summarize my unread emails",
    "write a python function that reverses a string",
    "open Safari",
    "what's my battery level",
    "hi",
    "tell me a joke",
)


def _model_literals(source: str) -> list[tuple[int, str]]:
    """String constants in `source` that look like model ids, docstrings aside.

    A docstring may recount the id that went stale; only code can route to it.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _MODEL_ID_RE.search(node.value)
    ]


def test_scanner_ignores_docstrings_but_not_code():
    source = (
        'def f():\n'
        '    """History: "Ling-3.0-tiny-oQ6e" was once pinned here."""\n'
        '    return "Ling-3.0-tiny-oQ4e"\n'
    )
    assert _model_literals(source) == [(3, "Ling-3.0-tiny-oQ4e")]


def test_scanner_recognizes_the_literal_that_caused_the_outage():
    found = _model_literals('_LING_WEB_MODEL = "Ling-3.0-tiny-oQ6e"\n')
    assert found == [(1, "Ling-3.0-tiny-oQ6e")]


def test_router_source_names_no_model_id():
    found = _model_literals(ROUTER_PATH.read_text())
    assert found == [], (
        "router.py must resolve models through config.role_to_model, never a "
        f"literal id (a literal goes stale when oMLX's served set changes): {found}"
    )


def _rostered_models() -> set[str]:
    cfg = models_config()
    rostered = set(cfg["roles"].values())
    for binding in (cfg.get("inference", {}).get("bindings", {}) or {}).values():
        if isinstance(binding, dict) and binding.get("model_id"):
            rostered.add(binding["model_id"])
    return rostered


@pytest.mark.parametrize("prompt", _PROMPTS)
def test_every_route_resolves_its_model_through_the_roster(prompt: str):
    sentinel = "Roster-Sentinel-Model"
    with patch("service.router.router.role_to_model", return_value=sentinel):
        decision = asyncio.run(route(prompt))
    assert decision.model == sentinel, (
        f"{prompt!r} reached {decision.model!r} without role_to_model: {decision.reason}"
    )


@pytest.mark.parametrize("prompt", _PROMPTS)
def test_routed_model_is_in_the_configured_roster(prompt: str):
    decision = asyncio.run(route(prompt))
    assert decision.model in _rostered_models(), (
        f"{prompt!r} routed to {decision.model!r}, which the roster does not name: "
        f"{decision.reason}"
    )


def test_web_routes_follow_a_reconfigured_agent_role():
    """The agent role, not a frozen id, decides where web lookups run."""
    configured = "Configured-Agent-test"
    with patch("service.router.router.role_to_model", return_value=configured):
        web = asyncio.run(route("what's happening in Iran"))
        opted_out = asyncio.run(route("latest news about Iran; do not browse"))
    assert web.model == configured
    assert web.direct_calls == [("web_search", {"query": "what's happening in Iran"})]
    assert opted_out.model == configured
    assert "web_search" in opted_out.forbidden_tools
    # And without any override the same routes land on the real agent model.
    assert asyncio.run(route("what's happening in Iran")).model == role_to_model("agent")
