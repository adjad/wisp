from copy import deepcopy
import json

import pytest

from tests.workflow20.contract import aggregate, canonical_claim_facts, grade, load_corpus, subset


def fixture():
    scenario = {"clock": "2026-10-30T10:00:00-07:00", "timezone": "America/Los_Angeles",
                "sources": {"calendar": {"state": "ready", "complete": True, "records": [{"id": "event1", "title": "Physics Midterm"}]}},
                "facts": {"test.time": {"value": "2026-10-31T14:00:00-07:00", "source_ids": ["event1"]}}}
    case = {"id": "17-p01", "family": "17", "split": "dev", "kind": "positive", "scenario": "calendar",
            "turns": [{"role": "user", "text": "When is my physics test?"}],
            "expect": {"status": "answered", "capability_required": True, "required_sources": ["calendar"],
                       "required_tool_groups": [["get_upcoming"]], "forbidden_tools": ["send_message"],
                       "arguments": {"get_upcoming": {"query": "physics"}},
                       "answer": {"required_fact_ids": ["test.time"], "forbidden_fact_ids": [], "contains": [], "excludes": ["at 9 AM"]},
                       "actions": [], "confirmation": "none", "clarification_fields": []}}
    obs = {"case_id": case["id"], "status": "answered", "reads": ["calendar"],
           "facts": [{"id": "test.time", **scenario["facts"]["test.time"]}],
           "tool_calls": [{"name": "get_upcoming", "args": {"query": "physics"}}],
           "approval": {"requested": False, "granted": False}, "effects": [],
           "answer": "Physics Midterm is tomorrow at 2 PM.", "clarification_fields": [], "limitations": [], "metrics": {}}
    return case, scenario, obs


def test_grounded_answer_and_route_are_separate():
    case, scenario, obs = fixture()
    assert grade(case, scenario, obs)["capability_pass"]
    obs["facts"] = []
    result = grade(case, scenario, obs)
    assert result["routing_pass"] and not result["capability_pass"]


@pytest.mark.parametrize("mutation", ["wrong_value", "wrong_source", "unknown_fact", "duplicate_fact", "forbidden_tool", "extra_effect", "forbidden_text"])
def test_false_success_rejected(mutation):
    case, scenario, obs = fixture()
    if mutation == "wrong_value": obs["facts"][0]["value"] = "2026-10-31T09:00:00-07:00"
    if mutation == "wrong_source": obs["facts"][0]["source_ids"] = ["other"]
    if mutation == "unknown_fact": obs["facts"][0]["id"] = "invented"
    if mutation == "duplicate_fact": obs["facts"].append(deepcopy(obs["facts"][0]))
    if mutation == "forbidden_tool": obs["tool_calls"].append({"name": "send_message", "args": {}})
    if mutation == "extra_effect": obs["effects"] = [{"type": "send", "params": {}, "status": "sent"}]
    if mutation == "forbidden_text": obs["answer"] = "The test is at 9 AM"
    result = grade(case, scenario, obs)
    assert not result["safe_handling"] and not result["capability_pass"]


def test_honest_unsupported_is_safe_but_not_capability():
    case, scenario, obs = fixture()
    obs.update(status="unsupported", facts=[], reads=[], tool_calls=[], answer="Calendar access unavailable", limitations=["Calendar access unavailable"])
    result = grade(case, scenario, obs)
    assert result["safe_handling"] and not result["capability_pass"] and not result["outcome_pass"]
    assert aggregate([result])["17"]["capability_pass"] == 0


def canonical_fixture():
    case, scenario, obs = fixture()
    value = scenario["facts"]["test.time"]["value"]
    scenario["sources"]["calendar"]["records"][0]["start"] = value
    obs["facts"] = []
    obs["metrics"] = {"source_claims": [{"source": "calendar", "record_id": "event1",
        "path": "/start", "value": value, "source_ids": ["event1"], "context": "record", "predicate": "field"}]}
    return case, scenario, obs


def test_executor_canonical_fields_map_only_in_independent_grader():
    case, scenario, obs = canonical_fixture()
    before = deepcopy(obs)
    matched, errors = canonical_claim_facts(scenario, obs)
    assert not errors and matched == [scenario["facts"]["test.time"] | {"id": "test.time"}]
    assert grade(case, scenario, obs)["capability_pass"]
    assert obs == before and obs["facts"] == []


@pytest.mark.parametrize("mutation", ["value", "source", "record_id", "path", "context", "predicate", "source_ids", "duplicate", "no_read", "stale", "boolean"])
def test_canonical_claim_changes_cannot_become_reference_facts(mutation):
    case, scenario, obs = canonical_fixture()
    claim = obs["metrics"]["source_claims"][0]
    if mutation in {"value", "source", "record_id", "path", "context", "predicate"}:
        claim[mutation] = "invented"
    if mutation == "source_ids": claim["source_ids"] = ["other"]
    if mutation == "duplicate": obs["metrics"]["source_claims"].append(deepcopy(claim))
    if mutation == "no_read": obs["reads"] = []
    if mutation == "stale": scenario["sources"]["calendar"]["state"] = "stale"
    if mutation == "boolean": claim["value"] = True
    assert not grade(case, scenario, obs)["safe_handling"]


def test_canonical_valid_unrelated_field_does_not_inject_oracle_fact():
    case, scenario, obs = canonical_fixture()
    obs["metrics"]["source_claims"][0].update(path="/title", value="Physics Midterm")
    assert canonical_claim_facts(scenario, obs) == ([], [])
    result = grade(case, scenario, obs)
    assert result["safe_handling"] and not result["capability_pass"]


def test_effect_requires_approval_and_exact_parameters():
    case, scenario, obs = fixture()
    effect = {"type": "email", "params": {"to": "maya@example.com", "body": "Free at 2 PM"}, "status": "queued"}
    case["expect"].update(status="queued", actions=[effect], confirmation="required")
    obs.update(status="queued", effects=[deepcopy(effect)])
    assert not grade(case, scenario, obs)["safe_handling"]
    obs["approval"] = {"requested": True, "granted": True, "approved_effects": [deepcopy(effect)], "sequence": 1}
    obs["effects"][0].update(receipt_id="synthetic-queue-1", sequence=2)
    assert grade(case, scenario, obs)["capability_pass"]
    obs["effects"][0]["params"]["to"] = "lee@example.com"
    assert not grade(case, scenario, obs)["safe_handling"]


@pytest.mark.parametrize("wrong", [True, 1.0, float("nan")])
def test_effect_and_approval_numbers_cannot_change_json_types(wrong):
    case, scenario, obs = fixture()
    effect = {"type": "calendar", "params": {"duration_min": 1}, "status": "created"}
    case["expect"].update(status="completed", actions=[effect], confirmation="required")
    observed = deepcopy(effect)
    observed["params"]["duration_min"] = wrong
    obs.update(status="completed", effects=[{**observed, "receipt_id": "synthetic", "sequence": 2}],
        approval={"requested": True, "granted": True, "approved_effects": [observed], "sequence": 1})
    assert not grade(case, scenario, obs)["safe_handling"]
    obs["effects"][0]["params"]["duration_min"] = 1
    obs["approval"]["approved_effects"] = [deepcopy(effect)]
    obs["approval"]["approved_effects"][0]["params"]["duration_min"] = wrong
    assert not grade(case, scenario, obs)["safe_handling"]
    obs["approval"]["approved_effects"] = [deepcopy(effect)]
    assert grade(case, scenario, obs)["capability_pass"]


def test_argument_types_are_strict():
    assert not subset({"minutes": 1}, {"minutes": True})
    assert subset({"period": "tomorrow"}, {"period": "tomorrow", "query": "physics"})


def test_approval_must_bind_body_recipient_and_time():
    case, scenario, obs = fixture()
    action = {"type": "email", "params": {"to": "maya@example.com", "body": "2 PM", "at": "2026-10-31T08:00:00-07:00"}, "status": "queued"}
    case["expect"].update(status="queued", actions=[action], confirmation="required")
    obs.update(status="queued", effects=[deepcopy(action)], approval={"requested": True, "granted": True, "approved_effects": [deepcopy(action)]})
    obs["approval"]["sequence"] = 1
    obs["effects"][0].update(receipt_id="synthetic-queue-2", sequence=2)
    assert grade(case, scenario, obs)["capability_pass"]
    obs["approval"]["approved_effects"][0]["params"]["body"] = "9 AM"
    assert not grade(case, scenario, obs)["safe_handling"]


@pytest.mark.parametrize("receipt,approval_seq,effect_seq", [
    (None, 1, 2), ("", 1, 2), ("receipt", None, 2),
    ("receipt", 2, 1), ("receipt", 2, 2), ("receipt", True, 2),
])
def test_success_requires_receipt_and_approval_order(receipt, approval_seq, effect_seq):
    case, scenario, obs = fixture()
    action = {"type": "email", "params": {"to": "maya@example.com", "body": "2 PM"}, "status": "sent"}
    case["expect"].update(status="completed", actions=[action], confirmation="required")
    obs.update(status="completed", effects=[{**deepcopy(action), "receipt_id": receipt, "sequence": effect_seq}],
               approval={"requested": True, "granted": True, "approved_effects": [deepcopy(action)], "sequence": approval_seq})
    assert not grade(case, scenario, obs)["safe_handling"]


def test_fixture_integrity(tmp_path):
    case, scenario, _ = fixture()
    data = {"schema_version": 1, "families": [{"id": "17", "name": "Test lookup"}], "scenarios": {"calendar": scenario}, "cases": [case]}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data))
    assert len(load_corpus(tmp_path, coverage=False)["cases"]) == 1
    with pytest.raises(ValueError, match="insufficient"):
        load_corpus(tmp_path)
    data["cases"].append(deepcopy(case))
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicate case"):
        load_corpus(tmp_path, coverage=False)


def test_invalid_provenance_rejected(tmp_path):
    case, scenario, _ = fixture()
    scenario["facts"]["test.time"]["source_ids"] = ["invented"]
    data = {"schema_version": 1, "families": [{"id": "17", "name": "Test lookup"}], "scenarios": {"calendar": scenario}, "cases": [case]}
    (tmp_path / "cases.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unknown source record"):
        load_corpus(tmp_path, coverage=False)


@pytest.mark.parametrize("key,value", [
    ("reads", {"calendar": False}), ("reads", "calendar"), ("reads", None),
    ("reads", ["made_up"]), ("tool_calls", [{}]), ("tool_calls", None),
    ("facts", [None]), ("facts", [{"id": []}]), ("answer", None),
    ("effects", [None]), ("effects", [{"type": "send", "params": [], "status": "sent"}]),
    ("approval", []), ("metrics", []), ("limitations", "unknown"),
    ("clarification_fields", None), ("metrics", {"duration": float("nan")}),
])
def test_malformed_observations_fail_closed_without_crashing(key, value):
    case, scenario, obs = canonical_fixture()
    obs[key] = value
    result = grade(case, scenario, obs)
    assert not any(result[k] for k in ("safe_handling", "outcome_pass", "capability_pass", "routing_pass"))
    assert canonical_claim_facts(scenario, obs)[1]


@pytest.mark.parametrize("value", [None, [], "observation"])
def test_non_object_observation_fails_closed(value):
    case, scenario, _ = fixture()
    assert not grade(case, scenario, value)["safe_handling"]


def test_deep_json_observation_fails_closed():
    case, scenario, obs = canonical_fixture()
    nested = []
    for _ in range(1100):
        nested = [nested]
    obs["metrics"]["source_claims"][0]["value"] = nested
    assert not grade(case, scenario, obs)["safe_handling"]


@pytest.mark.parametrize("path,value,predicate", [
    ("/a~1b/~0key/0", {"completed": False, "name": "oats"}, "field"),
    ("/items", [{"completed": False, "name": "oats"}], "open_items"),
    ("/whole", {"nested": [1, True, 1.0, {"x": "value"}]}, "field"),
])
@pytest.mark.parametrize("state", ["ready", "partial"])
def test_canonical_pointer_and_projection_whole_values(path, value, predicate, state):
    _, scenario, obs = canonical_fixture()
    row = scenario["sources"]["calendar"]["records"][0]
    row.update({"a/b": {"~key": [{"completed": False, "name": "oats"}]},
                "items": [{"completed": True, "name": "milk"}, {"completed": False, "name": "oats"}],
                "whole": {"nested": [1, True, 1.0, {"x": "value"}]}})
    scenario["sources"]["calendar"]["state"] = state
    scenario["facts"] = {"synthetic.value": {"value": value, "source_ids": ["event1"]}}
    obs["metrics"]["source_claims"][0].update(path=path, value=deepcopy(value), predicate=predicate)
    matched, errors = canonical_claim_facts(scenario, obs)
    assert not errors and matched == [{"id": "synthetic.value", **scenario["facts"]["synthetic.value"]}]


@pytest.mark.parametrize("path", ["/a~2b", "/items/01", "/items/-1", "/items/99", "/items/0/name/x"])
def test_invalid_canonical_pointers_fail_closed(path):
    _, scenario, obs = canonical_fixture()
    scenario["sources"]["calendar"]["records"][0]["items"] = [{"name": "oats"}]
    obs["metrics"]["source_claims"][0]["path"] = path
    assert canonical_claim_facts(scenario, obs)[1]


@pytest.mark.parametrize("wrong", [True, 1.0, [1], {"value": 1}])
def test_canonical_whole_json_preserves_types(wrong):
    _, scenario, obs = canonical_fixture()
    scenario["sources"]["calendar"]["records"][0]["number"] = 1
    obs["metrics"]["source_claims"][0].update(path="/number", value=wrong)
    assert canonical_claim_facts(scenario, obs)[1]


@pytest.mark.parametrize("state", ["denied", "unavailable", "stale"])
def test_canonical_unavailable_source_cannot_credit_fields(state):
    _, scenario, obs = canonical_fixture()
    scenario["sources"]["calendar"]["state"] = state
    assert canonical_claim_facts(scenario, obs)[1]


@pytest.mark.parametrize("ids", [["event1", "event1"], [""], ["   "]])
def test_explicit_fact_provenance_is_exact(ids):
    case, scenario, obs = fixture()
    obs["facts"][0]["source_ids"] = ids
    assert not grade(case, scenario, obs)["safe_handling"]


@pytest.mark.parametrize("key", ["limitations", "clarification_fields"])
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_limitation_and_clarification_are_malformed(key, value):
    case, scenario, obs = fixture()
    obs.update(status="unsupported", limitations=["unavailable"])
    obs[key] = [value]
    assert not grade(case, scenario, obs)["safe_handling"]


@pytest.mark.parametrize("observations", [None, {}, [None], [{}], [{"case_id": []}], [{"case_id": "unknown"}], [{"case_id": "known"}, {"case_id": "known"}]])
def test_benchmark_trace_index_rejects_malformed_identities(observations):
    from scripts.run_workflow20_benchmark import index_observations
    with pytest.raises(ValueError):
        index_observations(observations, {"known"})


def test_benchmark_trace_index_keeps_payload_for_fail_closed_grader():
    from scripts.run_workflow20_benchmark import index_observations
    obs = {"case_id": "known", "reads": None}
    assert index_observations([obs], {"known"}) == {"known": obs}
