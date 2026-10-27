"""A Brief card stays until it is handled, and a dismissed one stays gone.

Two problems this module answers, both code only (no model, no source read):

1. Identity. An item's content hash (``item["hash"]``) changes for reasons that
   are not a new message: a text conversation's window slides, a mail body is
   fetched after its subject, a sent-mail list arrives late. A dismissal keyed
   by that hash came back with the next hash. ``ask_key`` names the ask itself:
   the conversation (item id) plus the time of its latest incoming message. It
   changes only when someone writes again, which is a new ask.

2. Churn. Each cycle re-ranks from scratch, and a small model does not rank
   the same way twice, so the cards flip between runs. ``carry`` keeps a card
   that was already shown for as long as its ask stands (not dismissed, fired,
   answered, past due or withheld), adds new cards after it, and never
   reshuffles. ``reusable_drafts`` hands a shown reply's draft back so the
   model is not asked to write it again.
"""
from __future__ import annotations

# A card whose source dropped out of one read (a mail fetch that failed, a
# sync in progress) is kept this long before it is given up on.
MISSING_GRACE_S = 2 * 3600

_REPLY_METHODS = ("email_reply", "text_reply")
# Owned by deadlines.py and autoadd.py, which rebuild them every cycle.
_REBUILT_METHODS = ("block_time", "undo_add")


def ask_key(item: dict | None) -> str | None:
    """The stable name of what an item asks: item id + latest incoming message
    time (whole seconds). None for an item with no such time."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    ts = item.get("received_ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts != ts or abs(ts) == float("inf"):
        return None
    return f"ask:{item['id']}@{int(ts)}"


def card_ref(card: dict) -> str:
    return str(((card.get("sources") or [{}])[0] or {}).get("ref") or "")


def card_ask(card: dict) -> str | None:
    asks = card.get("asks") or []
    return asks[0] if asks and isinstance(asks[0], str) else None


def is_gone(record: dict, gone: set[str], suppressed: set[str]) -> bool:
    """A card (or to-do) the user dismissed, fired or otherwise handled: by its
    id, by its source fingerprint, or by its stable ask."""
    if record.get("id") in gone:
        return True
    prints = record.get("fingerprints") or []
    if prints and prints[0] in suppressed:
        return True
    return any(a in suppressed for a in record.get("asks") or [])


def reusable_drafts(previous: list[dict]) -> dict[tuple, str]:
    """{(item ref, ask, method): draft} for the reply cards already shown, so a
    cycle that finds the same ask again keeps the words it wrote."""
    out = {}
    for card in previous:
        method = (card.get("action") or {}).get("method")
        draft = (card.get("action") or {}).get("draft")
        ask = card_ask(card)
        if method in _REPLY_METHODS and draft and ask and card_ref(card):
            out[(card_ref(card), ask, method)] = draft
    return out


def _fresh_enough(card: dict, since: float, now: float) -> bool:
    seen = card.get("seen_at")
    seen = float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) else since
    return now - seen <= MISSING_GRACE_S


def carry(previous: list[dict], fresh: list[dict], *, items: dict[str, dict], withheld: set[str],
          gone: set[str], suppressed: set[str], done_todos: set[str], now: float, since: float,
          limit: int) -> list[dict]:
    """The cards to show: every earlier card whose ask still stands (refreshed
    from this cycle's card for the same item when there is one), then this
    cycle's new cards, alerts first and replies before the rest, cut to `limit`.

    items       this cycle's source items by id.
    withheld    item ids that must never surface (sensitive or withheld).
    gone        card ids the user dismissed or fired.
    suppressed  fingerprints and asks the user handled.
    done_todos  to-do ids marked done.
    since       when the earlier cards were last known good (for a source that
                is missing now, the grace runs from here).
    """
    fresh_by_ref = {card_ref(c): c for c in fresh if card_ref(c)}
    kept: list[dict] = []
    used: set[str] = set()
    for old in previous:
        action = old.get("action") or {}
        method = action.get("method")
        if method in _REBUILT_METHODS or old.get("auto_added") or old.get("auto_add_tap"):
            continue
        if is_gone(old, gone, suppressed) or old.get("todo_id") in done_todos:
            continue
        ref = card_ref(old)
        item = items.get(ref)
        if item is not None and ref in withheld:
            continue                     # never kept past a sensitive reading
        due = old.get("due_ts")
        if isinstance(due, (int, float)) and due <= now and method not in _REPLY_METHODS:
            continue                     # past due: at most an overdue to-do
        if item is None:
            # The source is not in this read: a short gap is a flaky read (a
            # failed fetch, a sync in progress), a long one means it is gone.
            if card_ask(old) is None or not _fresh_enough(old, since, now):
                continue
            kept.append({**old, "seen_at": old.get("seen_at") or since})
            continue
        ask = card_ask(old)
        if ask is not None and ask_key(item) != ask:
            continue                     # someone wrote again: a new ask, a new card
        if item.get("replied") and method in _REPLY_METHODS + ("read",) and item.get("source") in ("mail", "messages"):
            continue                     # answered elsewhere
        new = fresh_by_ref.get(ref)
        if new is not None and ref not in used:
            used.add(ref)
            new_method = (new.get("action") or {}).get("method")
            if new_method == method or (new_method in _REPLY_METHODS and method not in _REPLY_METHODS):
                # Fresh words and times, the identity the user already saw.
                kept.append({**new, "id": old["id"], "seen_at": now})
                continue
        # Same ask, no better way to act on it: the card the user saw stands.
        keep = {**old, "seen_at": now}
        if ask is None and ask_key(item):
            keep["asks"] = [ask_key(item)]
        kept.append(keep)
    new_cards = []
    for card in fresh:
        ref = card_ref(card)
        if ref and ref in used:
            continue
        if any(card_ref(k) == ref and ref for k in kept):
            card.pop("_sealed", None)    # the earlier card for this item stands
            continue
        new_cards.append({**card, "seen_at": now})
    merged = kept + new_cards
    # Stable tiers: alerts, then replies, then the rest, each in the order the
    # user has seen them, new cards after old.
    def tier(card: dict) -> int:
        if card.get("critical"):
            return 0
        return 1 if (card.get("action") or {}).get("method") in _REPLY_METHODS else 2
    merged.sort(key=tier)
    return merged[:limit]
