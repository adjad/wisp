"""Contract for the opt-in terse tool-schema mode (WISP_TERSE_TOOL_SCHEMAS=1).

Off by default and byte-identical to the registry's own schemas; on, the model
sees one-line descriptions and bare parameters while retrieval keeps indexing
the full prose. No model or server is touched.
"""
from __future__ import annotations

import copy
import json

import pytest

import service.tools  # noqa: F401  (registers every tool)
from service.router import reranker, semantic
from service.tools.registry import REGISTRY, Tool, tool_schemas
from service.tools.short_descriptions import KEEP_PARAMETER_PROSE, MAX_LEN, SHORT

FLAG = "WISP_TERSE_TOOL_SCHEMAS"


def _builtin_names() -> set[str]:
    return {n for n, t in REGISTRY.items()
            if getattr(t.func, "__module__", "").startswith("service.tools.")}


def _all_keys(node, found=None):
    """Every dict key at any depth, with the path kind, to find leftover prose."""
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            found.append((k, v))
            _all_keys(v, found)
    elif isinstance(node, list):
        for v in node:
            _all_keys(v, found)
    return found


def _descriptions_in_schema(params: dict) -> list:
    """Schema-position descriptions: a `description` key whose value is a string.
    A property merely NAMED description holds a dict and is not counted."""
    return [v for k, v in _all_keys(params) if k == "description" and isinstance(v, str)]


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def test_every_builtin_tool_has_a_short_line_within_budget():
    names = _builtin_names()
    assert len(names) >= 90
    assert names - set(SHORT) == set(), "built-in tools missing a SHORT entry"
    too_long = {n: len(SHORT[n]) for n in names if len(SHORT[n]) > MAX_LEN}
    assert too_long == {}
    assert all(SHORT[n].strip() and "\n" not in SHORT[n] for n in names)


def test_no_short_entry_for_a_nonexistent_tool():
    assert set(SHORT) - set(REGISTRY) == set()


def test_flag_off_emits_the_registry_schemas_unchanged():
    emitted = tool_schemas()
    expected = [t.schema() for t in REGISTRY.values() if not t.unavailable_reason]
    assert emitted == expected
    assert json.dumps(emitted) == json.dumps(expected)


@pytest.mark.parametrize("value", ["0", "", "no", "off", "maybe"])
def test_non_enabling_values_keep_full_schemas(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert tool_schemas(["run_shell"]) == [REGISTRY["run_shell"].schema()]


def test_flag_on_uses_short_description_and_strips_parameter_prose(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    schemas = tool_schemas()
    assert {s["function"]["name"] for s in schemas} == {
        n for n, t in REGISTRY.items() if not t.unavailable_reason}
    for s in schemas:
        fn = s["function"]
        tool = REGISTRY[fn["name"]]
        if fn["name"] not in SHORT:
            # skill/MCP/use_skill tools were never trained terse: untouched
            assert fn["description"] == tool.description
            assert fn["parameters"] == tool.parameters
            continue
        assert fn["description"] == SHORT[fn["name"]]
        kept = KEEP_PARAMETER_PROSE.get(fn["name"], frozenset())
        stripped = {k: v for k, v in (fn["parameters"].get("properties") or {}).items() if k not in kept}
        assert _descriptions_in_schema({"properties": stripped}) == [], fn["name"]
        assert "description" not in fn["parameters"], fn["name"]
        # structure other than prose is untouched
        assert fn["parameters"].get("required") == tool.parameters.get("required")
        for prop, spec in (tool.parameters.get("properties") or {}).items():
            emitted = fn["parameters"]["properties"][prop]
            for key in spec:
                if key != "description":
                    assert emitted.get(key) == spec[key], (fn["name"], prop, key)


def test_enums_required_and_nested_properties_survive(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    reminder = tool_schemas(["add_reminder"])[0]["function"]["parameters"]
    assert reminder["properties"]["kind"]["enum"] == ["reminder", "assignment", "exam", "meeting", "event"]
    assert reminder["required"] == ["title", "when_iso"]
    # create_tool nests objects under items and has an argument literally named
    # "description": the name must stay, the prose must go.
    created = tool_schemas(["create_tool"])[0]["function"]["parameters"]
    assert "description" in created["properties"]
    nested = created["properties"]["parameters"]["items"]["properties"]
    assert set(nested) == {"name", "type", "description", "required"}
    assert _descriptions_in_schema(created) == []


def test_registry_objects_are_not_mutated(monkeypatch):
    before = {n: (t.description, copy.deepcopy(t.parameters)) for n, t in REGISTRY.items()}
    monkeypatch.setenv(FLAG, "1")
    out = tool_schemas()
    # mutating the emitted copy must not reach the registry either
    for s in out:
        s["function"]["parameters"].setdefault("properties", {})["__probe__"] = {}
    after = {n: (t.description, t.parameters) for n, t in REGISTRY.items()}
    assert after == before


def test_unknown_tool_falls_back_to_full_description(monkeypatch):
    registered = Tool("zz_skill_probe", "A long skill description. " * 8,
                      {"type": "object", "properties": {"x": {"type": "string", "description": "prose"}}},
                      "compute", lambda **_: "")
    monkeypatch.setitem(REGISTRY, "zz_skill_probe", registered)
    monkeypatch.setenv(FLAG, "1")
    fn = tool_schemas(["zz_skill_probe"])[0]["function"]
    # A tool without a terse line keeps every word, parameters included:
    # nothing else would tell the model an MCP parameter's rules.
    assert fn["description"] == registered.description
    assert fn["parameters"] == registered.parameters


def test_literal_values_named_description_are_data_not_prose():
    from service.tools.registry import _strip_parameter_descriptions
    schema = {"type": "object", "description": "drop", "properties": {
        "mode": {"type": "object", "description": "drop", "default": {"description": "keep"},
                 "enum": [{"description": "keep"}], "const": {"description": "keep"},
                 "examples": [{"description": "keep"}]},
        "items_prop": {"type": "array", "items": {"type": "string", "description": "drop"}},
        "choice": {"anyOf": [{"type": "string", "description": "drop"}, {"type": "integer"}]},
        "description": {"type": "string", "description": "drop"},
    }}
    out = _strip_parameter_descriptions(schema)
    mode = out["properties"]["mode"]
    assert "description" not in out and "description" not in mode
    assert mode["default"] == mode["const"] == {"description": "keep"}
    assert mode["enum"] == mode["examples"] == [{"description": "keep"}]
    assert out["properties"]["items_prop"]["items"] == {"type": "string"}
    assert out["properties"]["choice"]["anyOf"] == [{"type": "string"}, {"type": "integer"}]
    assert out["properties"]["description"] == {"type": "string"}
    assert schema["properties"]["mode"]["description"] == "drop"   # input untouched


def test_safety_and_format_parameters_keep_their_prose(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    for name, params in KEEP_PARAMETER_PROSE.items():
        props = REGISTRY[name].parameters["properties"]
        assert params <= set(props), (name, params - set(props))
        emitted = tool_schemas([name])[0]["function"]["parameters"]["properties"]
        for param in params:
            assert emitted[param] == props[param], (name, param)
    assert "ONLY on a retry" in tool_schemas(["send_email"])[0]["function"]["parameters"][
        "properties"]["confirmed_self_send"]["description"]


def test_no_terse_line_claims_a_confirmation_policy_may_skip():
    assert not [n for n, line in SHORT.items() if "confirm-gated" in line.lower()]


def test_retrieval_text_is_identical_with_flag_on(monkeypatch):
    docs_off = {n: semantic._docs(t) for n, t in REGISTRY.items()}
    signature_off = reranker._signature()
    monkeypatch.setenv(FLAG, "1")
    tool_schemas()  # terse rendering has run; retrieval must not notice
    assert {n: semantic._docs(t) for n, t in REGISTRY.items()} == docs_off
    assert reranker._signature() == signature_off
    # and the indexed text is the full prose, not the one-liner
    assert REGISTRY["view_emails"].description in docs_off["view_emails"][0]
    assert SHORT["view_emails"] not in docs_off["view_emails"][0]


def test_model_led_discovery_schemas_follow_terse_mode(monkeypatch):
    # Discovery hands schemas to the model through ToolSpec.schema(), not
    # tool_schemas(); a full schema there would show the model a mix of styles.
    from service.router.model_led import registry_specs
    from service.tools.registry import REGISTRY, tool_schemas
    spec = {s.name: s for s in registry_specs(REGISTRY)}["send_email"]
    monkeypatch.setenv("WISP_TERSE_TOOL_SCHEMAS", "0")
    assert spec.schema() == REGISTRY["send_email"].schema()
    monkeypatch.setenv("WISP_TERSE_TOOL_SCHEMAS", "1")
    assert spec.schema() == tool_schemas(["send_email"])[0]
    assert spec.schema()["function"]["description"] != REGISTRY["send_email"].description
