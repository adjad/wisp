"""Exact payload and readback contracts for durable native Reminders actions."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

KINDS = frozenset({"create_reminder", "update_reminder", "complete_reminder", "delete_reminder"})
COMMITMENT_KINDS = frozenset({"reminder", "assignment", "exam", "meeting", "event"})
_ACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value: object) -> bool:
    return type(value) in (int, float) and 0 < value < 253402300800


def valid_due_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def due_timestamp(value: dict) -> float:
    """Index a date at its last minute so it is due through the whole day.

    This is not an alert time. Native writes carry the floating date itself,
    and the notification scheduler excludes all-day records.
    """
    if value.get('all_day') is True and valid_due_date(value.get('due_date')):
        return (datetime.fromisoformat(value['due_date']) + timedelta(days=1, minutes=-1)).timestamp()
    return float(value['due_ts'])


def expected_date_fields(row: dict) -> dict:
    """Bind floating-date targets separately from timed-minute targets."""
    if not row.get('all_day'):
        return {}
    value = row.get('due_date') or datetime.fromtimestamp(row['when_ts']).date().isoformat()
    if not valid_due_date(value):
        raise ValueError('invalid expected all-day date')
    return {'expected_due_date': value}


def expected_matches(row, payload: dict) -> bool:
    return (bool(row['all_day']) == bool(payload.get('expected_due_date'))
            and (not payload.get('expected_due_date')
                 or (dict(row).get('due_date') or datetime.fromtimestamp(row['when_ts']).date().isoformat())
                    == payload['expected_due_date']))


def validate_payload(kind: str, payload: dict) -> dict:
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("unsupported reminder action")
    keys = {"action_id", "type"}
    if kind == "create_reminder":
        keys |= {"title", "commitment_kind"}
        if payload.get('all_day') is True:
            keys |= {'all_day', 'due_date'}
            if not valid_due_date(payload.get('due_date')):
                raise ValueError('invalid all-day reminder date')
        else:
            keys.add('due_ts')
    else:
        keys |= {"source_id", "expected_title", "expected_due_ts"}
        if 'expected_due_date' in payload:
            keys.add('expected_due_date')
            if not valid_due_date(payload['expected_due_date']) or not _timestamp(payload.get('expected_due_ts')):
                raise ValueError('invalid expected all-day target')
        if kind == "update_reminder":
            keys.add('title')
            if payload.get('all_day') is True:
                keys |= {'all_day', 'due_date'}
                if not valid_due_date(payload.get('due_date')):
                    raise ValueError('invalid all-day reminder date')
            else:
                keys.add('due_ts')
    if (set(payload) != keys or payload.get("type") != kind
            or not isinstance(payload.get("action_id"), str)
            or _ACTION_ID.fullmatch(payload["action_id"]) is None
            or ("source_id" in keys and not _text(payload.get("source_id")))
            or ("expected_title" in keys and not _text(payload.get("expected_title")))
            or ("expected_due_ts" in keys and not _timestamp(payload.get("expected_due_ts"))
                # Only a delete may target a reminder with no due date, and it says so
                # with an explicit null; the native writer then requires it still be undated.
                and not (kind == "delete_reminder" and payload.get("expected_due_ts") is None))
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
            expected_keys.add('title')
            expected_keys |= ({'all_day', 'due_date'} if payload.get('all_day') is True else {'due_ts'})
        elif kind == "complete_reminder":
            expected_keys.add("is_completed")
        else:
            expected_keys.add("is_absent")
        if set(result) != expected_keys or not _text(result.get("source_id")):
            raise ValueError("incomplete native readback")
        if kind != "create_reminder" and result["source_id"] != payload["source_id"]:
            raise ValueError("native identity changed")
        if kind in {"create_reminder", "update_reminder"}:
            if payload.get('all_day') is True:
                if (result['title'] != payload['title'] or result['all_day'] is not True
                        or not valid_due_date(result['due_date'])
                        or result['due_date'] != payload['due_date']):
                    raise ValueError('native all-day date was not read back')
            elif (result["title"] != payload["title"] or not _timestamp(result["due_ts"])
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
