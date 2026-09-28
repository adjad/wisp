"""Exact payload and readback contracts for durable native Reminders actions."""
from __future__ import annotations

import re

KINDS = frozenset({"create_reminder", "update_reminder", "complete_reminder", "delete_reminder"})
COMMITMENT_KINDS = frozenset({"reminder", "assignment", "exam", "meeting", "event"})
_ACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value: object) -> bool:
    return type(value) in (int, float) and 0 < value < 253402300800


def validate_payload(kind: str, payload: dict) -> dict:
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("unsupported reminder action")
    keys = {"action_id", "type"}
    if kind == "create_reminder":
        keys |= {"title", "due_ts", "commitment_kind"}
    else:
        keys |= {"source_id", "expected_title", "expected_due_ts"}
        if kind == "update_reminder":
            keys |= {"title", "due_ts"}
    if (set(payload) != keys or payload.get("type") != kind
            or not isinstance(payload.get("action_id"), str)
            or _ACTION_ID.fullmatch(payload["action_id"]) is None
            or ("source_id" in keys and not _text(payload.get("source_id")))
            or ("expected_title" in keys and not _text(payload.get("expected_title")))
            or ("expected_due_ts" in keys and not _timestamp(payload.get("expected_due_ts")))
            or ("title" in keys and not _text(payload.get("title")))
            or ("due_ts" in keys and not _timestamp(payload.get("due_ts")))):
        raise ValueError("invalid exact reminder payload")
    if kind == "create_reminder" and (not isinstance(payload["commitment_kind"], str)
                                       or payload["commitment_kind"] not in COMMITMENT_KINDS):
        raise ValueError("invalid reminder commitment kind")
    return dict(payload)


def validate_result(kind: str, payload: dict, result: dict) -> dict:
    """Accept only an exact, operation-specific native result.

    ``succeeded`` requires readback values. A write API's bare success flag is
    not verification. Unknown/failed results carry no asserted native state.
    """
    if not isinstance(result, dict) or set(result) < {"ok", "status", "error"}:
        raise ValueError("incomplete reminder result")
    if (type(result["ok"]) is not bool or not isinstance(result["error"], str)
            or not isinstance(result["status"], str)):
        raise ValueError("invalid reminder result types")
    if result["ok"]:
        if result["status"] != "succeeded" or result["error"]:
            raise ValueError("contradictory reminder success")
        expected_keys = {"ok", "status", "error", "source_id"}
        if kind in {"create_reminder", "update_reminder"}:
            expected_keys |= {"title", "due_ts"}
        elif kind == "complete_reminder":
            expected_keys.add("is_completed")
        else:
            expected_keys.add("is_absent")
        if set(result) != expected_keys or not _text(result.get("source_id")):
            raise ValueError("incomplete native readback")
        if kind != "create_reminder" and result["source_id"] != payload["source_id"]:
            raise ValueError("native identity changed")
        if kind in {"create_reminder", "update_reminder"}:
            if (result["title"] != payload["title"] or not _timestamp(result["due_ts"])
                    or int(result["due_ts"] // 60) != int(payload["due_ts"] // 60)):
                raise ValueError("native values do not match requested reminder")
        elif kind == "complete_reminder":
            if result["is_completed"] is not True:
                raise ValueError("completion was not read back")
        elif result["is_absent"] is not True:
            raise ValueError("deletion was not read back")
    else:
        if (set(result) != {"ok", "status", "error"}
                or result["status"] not in {"failed", "unknown"}
                or not result["error"].strip()):
            raise ValueError("unknown or failed result cannot assert native state")
    return dict(result)
