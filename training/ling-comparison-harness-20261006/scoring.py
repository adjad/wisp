"""Strict, dependency-free validation of the frozen intent schema and diagnostics."""
import json
import re
from collections import Counter
from datetime import date


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strict_json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def schema_errors(value, schema, path="$"):
    """Only this pinned schema's keywords are supported; fail closed on new ones."""
    supported = {"type", "enum", "properties", "required", "additionalProperties", "items",
                 "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "minimum", "maximum"}
    if set(schema) - supported:
        raise ValueError("unsupported schema keywords")
    kinds = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
             "string": lambda x: isinstance(x, str), "integer": lambda x: type(x) is int,
             "boolean": lambda x: type(x) is bool}
    errors = []
    if "type" in schema and not kinds[schema["type"]](value):
        return [path + ": wrong type"]
    if "enum" in schema and canonical(value) not in [canonical(x) for x in schema["enum"]]:
        errors.append(path + ": outside enum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [path + ": missing " + key for key in schema.get("required", []) if key not in value]
        if schema.get("additionalProperties") is False:
            errors += [path + ": unexpected " + key for key in value if key not in props]
        for key in value.keys() & props.keys():
            errors += schema_errors(value[key], props[key], path + "." + key)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", float("inf")):
            errors.append(path + ": too many items")
        if schema.get("uniqueItems") and len({canonical(x) for x in value}) != len(value):
            errors.append(path + ": duplicate items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            errors.append(path + ": wrong string length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(path + ": wrong pattern")
    if type(value) is int and not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
        errors.append(path + ": outside range")
    return errors


CAPABILITIES = {
    "calendar": {"overview": {"time", "query", "account"}, "records": {"time", "query", "account"},
                 "free_time": {"time", "query", "account", "minutes"}},
    "reminders": {"overview": {"scope", "query"}, "records": {"scope", "query"}},
    "email": {"overview": {"time", "account", "unread", "count"},
              "records": {"time", "account", "unread", "count", "query"}},
    "messages": {"overview": {"time", "conversation", "count"}, "records": {"time", "query", "count"}},
    "notes": {"overview": {"time", "query", "count"}, "records": {"time", "query", "count"}},
}


def semantic_errors(intent):
    errors = []
    sources = intent["sources"]
    if (intent["kind"] == "read") != bool(sources):
        errors.append("read requires sources; other kinds forbid sources")
    if len({s["domain"] for s in sources}) != len(sources):
        errors.append("duplicate source domain")
    for source in sources:
        domain, operation = source["domain"], source["operation"]
        if domain in intent["excluded_sources"]:
            errors.append("excluded source requested")
        allowed = CAPABILITIES[domain].get(operation)
        if allowed is None:
            errors.append("unsupported source operation")
        elif set(source) - {"domain", "operation"} - allowed:
            errors.append("unsupported source arguments")
        if "time" in source:
            t = source["time"]
            if not t or (set(t) != {"start", "end"} and len(t) != 1) or (set(t) & {"start", "end"} and set(t) != {"start", "end"}):
                errors.append("time must use one representation or paired range")
            try:
                for key in ("date", "start", "end"):
                    if key in t:
                        date.fromisoformat(t[key])
                if "month" in t:
                    date.fromisoformat(t["month"] + "-01")
                if "start" in t and "end" in t and t["start"] > t["end"]:
                    errors.append("reversed date range")
            except ValueError:
                errors.append("invalid calendar date")
    return errors


def normalized(intent):
    out = dict(intent)
    out["sources"] = sorted(intent["sources"], key=canonical)
    out["excluded_sources"] = sorted(intent["excluded_sources"])
    out["unsupported_constraints"] = sorted(intent["unsupported_constraints"])
    return out


def score(text, expected, schema, usable=True):
    metrics = {key: False for key in ("json_valid", "schema_valid", "semantic_valid", "exact",
        "normalized_exact", "kind_match", "source_match", "operation_match", "arguments_match",
        "exclusions_match", "unsupported_match", "unsupported_presence_match")}
    result = {"metrics": metrics, "parse_error": None, "schema_errors": [], "semantic_errors": [],
              "unexpected_source_intent": False, "excluded_source_intent": False, "parsed": None}
    try:
        actual = strict_json(text)
        result["parsed"] = actual
        metrics["json_valid"] = True
    except (ValueError, TypeError):
        result["parse_error"] = "invalid strict JSON"
        return result
    result["schema_errors"] = schema_errors(actual, schema)
    metrics["schema_valid"] = not result["schema_errors"]
    if not metrics["schema_valid"]:
        return result
    result["semantic_errors"] = semantic_errors(actual)
    metrics["semantic_valid"] = not result["semantic_errors"]
    adomains = Counter(s["domain"] for s in actual["sources"])
    edomains = Counter(s["domain"] for s in expected["sources"])
    result["unexpected_source_intent"] = bool(adomains - edomains)
    result["excluded_source_intent"] = bool(set(adomains) & (set(expected["excluded_sources"]) | set(actual["excluded_sources"])))
    if not usable:
        return result  # timeout/length/incomplete receipts cannot count as successes
    metrics["kind_match"] = actual["kind"] == expected["kind"]
    metrics["source_match"] = adomains == edomains
    metrics["operation_match"] = Counter((s["domain"], s["operation"]) for s in actual["sources"]) == Counter((s["domain"], s["operation"]) for s in expected["sources"])
    metrics["arguments_match"] = canonical(normalized(actual)["sources"]) == canonical(normalized(expected)["sources"])
    metrics["exclusions_match"] = actual["excluded_sources"] == expected["excluded_sources"]
    metrics["unsupported_presence_match"] = bool(actual["unsupported_constraints"]) == bool(expected["unsupported_constraints"])
    metrics["unsupported_match"] = actual["unsupported_constraints"] == expected["unsupported_constraints"]
    metrics["exact"] = metrics["semantic_valid"] and canonical(actual) == canonical(expected)
    metrics["normalized_exact"] = metrics["semantic_valid"] and canonical(normalized(actual)) == canonical(normalized(expected))
    return result
