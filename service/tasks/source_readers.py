"""Adapters over recorded source data. No model-selected tools or fuzzy IDs."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re

from service.tasks.references import Candidate, SourceBatch, SourceRef


_MAIL_FAILURE_REASONS = {
    "duplicate_account_labels": (
        "Mail can’t safely tell two accounts apart because they share an account label. "
        "In Mail, open Mail > Settings > Accounts and rename one of the duplicate "
        "account labels so every account name is unique. Then refresh Wisp and try again. "
        "Nothing was sent."
    ),
}


def _contains(value: str, query: str) -> bool:
    tokens = re.findall(r"[\w@.+-]+", query.casefold())
    return bool(tokens) and all(re.search(rf"\b{re.escape(token)}\b", value.casefold())
                                for token in tokens)


class MailReader:
    kind = "email"

    def __init__(self, rows: list[dict], *, synced_at: float = 0,
                 accounts: list[str] | None = None, failed_accounts: list[str] | None = None,
                 failure_reason: str = "", available: bool = True, complete: bool = True,
                 deep_rows: list[dict] | None = None):
        self.rows, self.synced_at = rows, synced_at
        self.accounts, self.failed_accounts = accounts or [], failed_accounts or []
        self.failure_reason = failure_reason
        self.available, self.complete = available, complete
        self.deep_rows = deep_rows or []

    def candidates(self, ref: SourceRef, *, now: datetime) -> SourceBatch:
        hints = ref.hints
        account = hints.get("account", "")
        scope = "the recent synced inbox messages" + (f" in {account}" if account else " across synced accounts")
        complete = self.complete
        if account:
            complete = any(a.casefold() == account.casefold() for a in self.accounts) and not any(
                a.casefold() == account.casefold() for a in self.failed_accounts)
        batch = SourceBatch(available=self.available, complete=complete,
                            scope=scope, synced_at=self.synced_at,
                            reason=_MAIL_FAILURE_REASONS.get(
                                self.failure_reason,
                                "Mail’s recent-message cache is unavailable, stale or only partly synced. "
                                "Nothing was sent. Refresh Mail and try again."))
        if not self.available or not complete:
            return batch
        def matches(row):
            if account and str(row.get("account", "")).casefold() != account.casefold():
                return False
            if hints.get("sender") and not _contains(str(row.get("sender", "")), hints["sender"]):
                return False
            query = hints.get("topic", ref.query)
            if query and not _contains(str(row.get("subject", "")), query):
                return False
            if day := hints.get("day"):
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if day == "yesterday":
                    start -= timedelta(days=1)
                if not start.timestamp() <= float(row.get("ts") or 0) < (start + timedelta(days=1)).timestamp():
                    return False
            return True
        rows = [r for r in self.rows if matches(r)]
        # Deep header hits are explicitly visible but cannot manufacture IDs.
        if not rows:
            rows = [{**r, "message_id": ""} for r in self.deep_rows if matches(r)]
        for row in sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True):
            message_id, acct = str(row.get("message_id") or ""), str(row.get("account") or "")
            key = json.dumps([row.get("account_id") or acct, message_id or [row.get("ts"), row.get("sender"), row.get("subject")]])
            fields = {k: row.get(k) for k in ("message_id", "account", "account_id", "sender", "to", "subject", "ts")}
            fields["body_digest"] = hashlib.sha256(str(row.get("body") or "").encode()).hexdigest()
            fields["cache_synced_at"] = self.synced_at
            date = datetime.fromtimestamp(float(row.get("ts") or 0)).strftime("%b %d %H:%M")
            age = (now.date() - datetime.fromtimestamp(float(row.get("ts") or 0)).date()).days
            if age in {0, 1}:
                date = ("today" if age == 0 else "yesterday") + " " + date
            label = f"{row.get('subject') or '(no subject)'} — {row.get('sender') or '?'} — {acct} — {date}"
            batch.candidates.append(Candidate(self.kind, key, label, fields, bool(message_id and acct)))
        return batch


def current_mail_reader() -> MailReader:
    from service.tools import email_tools as mail
    meta = mail.raw_reference_metadata()
    # Deep headers only augment a fresh scoped source; they don't prove that
    # an unavailable raw source is an empty inbox.
    deep = mail.header_rows()
    deep += [{"ts": ts, "account": account, "sender": sender, "subject": subject}
             for ts, account, sender, subject, _read in mail._parse_history()]
    return MailReader(mail._parse_raw(), deep_rows=deep, **meta)


class CalendarReader:
    """Contract-test adapter; existing reminder resolution is not migrated."""
    kind = "calendar"

    def __init__(self, rows: list[dict], *, available: bool = True):
        self.rows, self.available = rows, available

    def candidates(self, ref: SourceRef, *, now: datetime) -> SourceBatch:
        from service.tasks.temporal import resolve_event_reference
        rows = resolve_event_reference(ref.query, self.rows)
        return SourceBatch([
            Candidate(self.kind, json.dumps([r.get("source_id") or r.get("id"), r.get("when_ts")]),
                      str(r.get("title") or ""), dict(r), bool(r.get("source_id") or r.get("id")))
            for r in rows], available=self.available, scope="the synced calendar events")
