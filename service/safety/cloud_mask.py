"""Reversible masking of obvious identifiers for requests that go to a cloud model.

Before a request leaves the Mac for a cloud provider, emails, phone numbers and
card-like numbers in its messages (system, user, assistant and tool results,
tool-call arguments included) are replaced with stable placeholders such as
``[EMAIL_1]`` and ``[PHONE_1]``. The same value always gets the same placeholder
within one conversation. Everything the model sends back is unmasked before
Wisp acts on it or shows it: reply text (including streamed text, where a
placeholder can be split across chunks), reasoning text, and tool-call arguments
(before the tool runs). A placeholder the model invents that is not in the
mapping is left as it is.

The mapping lives only in this process's memory, keyed per conversation. It is
never sent anywhere, never written to disk and never logged: nothing in this
module logs, and a Masker's repr, pickle and copy do not carry it.

Detection is local and regex only. It is lossy by design and never a guarantee:
it reduces what leaves the Mac, it does not prove that nothing identifying does.
Credential redaction (service/safety/redaction.py) is separate and still runs.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from typing import Any, Iterable, Iterator

_EMAIL = r"(?<![\w.+%-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}"
_CARD = r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"
# An international number must start with "+"; a national one is the 3-3-4 shape.
_PHONE_INTL = r"(?<![\w+])\+\d{1,3}(?:[ .\-]?\(?\d{1,4}\)?){2,5}(?!\d)"
_PHONE_NATIONAL = r"(?<![\w+.-])(?:\(\d{3}\)[ ]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
_DETECT = re.compile(
    f"(?P<email>{_EMAIL})|(?P<card>{_CARD})|(?P<phone>{_PHONE_INTL}|{_PHONE_NATIONAL})")
_NAME_KIND = "NAME"
_PLACEHOLDER = re.compile(r"\[(EMAIL|PHONE|NUMBER|NAME)_(\d+)\]")
# The longest placeholder is "[NUMBER_999999]"; anything longer is not one.
_PARTIAL = re.compile(r"\[[A-Z]{0,6}(?:_\d{0,6})?")
_MAX_PARTIAL = 16
_MAX_DEPTH = 40
_MAX_CONVERSATIONS = 32


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


class Masker:
    """One conversation's mapping between real values and placeholders."""

    __slots__ = ("_forward", "_reverse", "_counts", "_names")

    def __init__(self) -> None:
        self._forward: dict[tuple[str, str], str] = {}
        self._reverse: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._names: list[str] = []

    # The mapping must never leak through a log line, a debug dump or a copy.
    def __repr__(self) -> str:
        return f"<Masker entries={len(self._reverse)}>"

    def __reduce_ex__(self, protocol):
        raise TypeError("A Masker is never serialized")

    def __copy__(self):
        raise TypeError("A Masker is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("A Masker is never copied")

    def __len__(self) -> int:
        return len(self._reverse)

    def add_names(self, names: Iterable[str]) -> None:
        """Names an on-device helper already knows (a contact, the user): masked as
        [NAME_n] on whole-word, case-sensitive matches. Short names are ignored."""
        for name in names or ():
            name = name.strip() if isinstance(name, str) else ""
            if len(name) >= 3 and name not in self._names:
                self._names.append(name)
        self._names.sort(key=len, reverse=True)

    def _placeholder(self, kind: str, key: str, original: str) -> str:
        found = self._forward.get((kind, key))
        if found is not None:
            return found
        n = self._counts.get(kind, 0) + 1
        self._counts[kind] = n
        token = f"[{kind}_{n}]"
        self._forward[(kind, key)] = token
        self._reverse[token] = original
        return token

    def _replace(self, match: re.Match) -> str:
        text = match.group(0)
        if match.group("email"):
            return self._placeholder("EMAIL", text.lower(), text)
        digits = re.sub(r"\D", "", text)
        if match.group("card"):
            if 13 <= len(digits) <= 19 and _luhn(digits):
                return self._placeholder("NUMBER", digits, text)
            return text
        if 8 <= len(digits) <= 15:
            return self._placeholder("PHONE", ("+" if text.startswith("+") else "") + digits, text)
        return text

    def mask(self, text: str) -> str:
        if not isinstance(text, str) or len(text) < 5:
            return text
        out = _DETECT.sub(self._replace, text)
        for name in self._names:
            if name in out:
                token = self._placeholder(_NAME_KIND, name, name)
                out = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", lambda _m: token, out)
        return out

    def unmask(self, text: str) -> str:
        if not isinstance(text, str) or "[" not in text or not self._reverse:
            return text
        return _PLACEHOLDER.sub(lambda m: self._reverse.get(m.group(0), m.group(0)), text)

    def unmask_obj(self, value: Any, _depth: int = 0) -> Any:
        """Unmask every string (and dict key) inside JSON-like data, such as tool-call
        arguments. Returns new containers; the input is not changed."""
        if _depth > _MAX_DEPTH:
            return value
        if isinstance(value, str):
            return self.unmask(value)
        if isinstance(value, dict):
            return {(self.unmask(k) if isinstance(k, str) else k): self.unmask_obj(v, _depth + 1)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.unmask_obj(v, _depth + 1) for v in value]
        return value

    def mask_obj(self, value: Any, _depth: int = 0) -> Any:
        if _depth > _MAX_DEPTH:
            return value
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {k: self.mask_obj(v, _depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.mask_obj(v, _depth + 1) for v in value]
        return value

    def _json_string(self, raw: str, transform) -> str:
        """A JSON-encoded string (tool-call arguments) through `transform`, as JSON.
        Unchanged text is returned byte for byte."""
        if not isinstance(raw, str):
            return raw
        try:
            parsed = json.loads(raw)
        except ValueError:
            return transform(raw)
        changed = self.mask_obj(parsed) if transform == self.mask else self.unmask_obj(parsed)
        if changed == parsed:
            return raw
        return json.dumps(changed, ensure_ascii=False)

    def unmask_arguments(self, raw: str) -> str:
        return self._json_string(raw, self.unmask)

    def mask_arguments(self, raw: str) -> str:
        return self._json_string(raw, self.mask)

    # ---- message level ----

    def _mask_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return self.mask(content)
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    masked = self.mask(part["text"])
                    part = part if masked == part["text"] else {**part, "text": masked}
                parts.append(part)
            return parts if any(a is not b for a, b in zip(parts, content)) else content
        return content

    def mask_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The request's messages with identifiers masked (copies of changed rows
        only; every other key, replay reasoning included, is carried by reference)."""
        out = []
        for message in messages:
            if not isinstance(message, dict):
                out.append(message)
                continue
            new = dict(message)
            if "content" in new:
                new["content"] = self._mask_content(new["content"])
            calls = new.get("tool_calls")
            if isinstance(calls, list):
                masked_calls = []
                for call in calls:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                        call = {**call, "function": {**fn, "arguments": self.mask_arguments(fn["arguments"])}}
                    masked_calls.append(call)
                new["tool_calls"] = masked_calls
            changed = new.get("content") != message.get("content") or new.get("tool_calls") != calls
            out.append(new if changed else message)
        return out

    def unmask_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Unmask a model reply in place: content, reasoning text and tool-call
        arguments. Provider reasoning_details are left exactly as received."""
        if not self._reverse or not isinstance(message, dict):
            return message
        for key in ("content", "reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                message[key] = self.unmask(value)
            elif isinstance(value, list):
                message[key] = [
                    {**p, "text": self.unmask(p["text"])} if isinstance(p, dict) and isinstance(p.get("text"), str) else p
                    for p in value]
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = self.unmask_arguments(fn["arguments"])
        return message

    def stream(self) -> "StreamUnmasker":
        return StreamUnmasker(self)


class StreamUnmasker:
    """Unmasks streamed text. A partial placeholder at a chunk's end ("[EMA") is held
    until the next chunk completes it, or until `flush`."""

    __slots__ = ("_masker", "_held")

    def __init__(self, masker: Masker) -> None:
        self._masker = masker
        self._held = ""

    def feed(self, chunk: str) -> str:
        text = self._held + (chunk or "")
        self._held = ""
        cut = text.rfind("[")
        if cut != -1:
            tail = text[cut:]
            if len(tail) <= _MAX_PARTIAL and _PARTIAL.fullmatch(tail):
                text, self._held = text[:cut], tail
        return self._masker.unmask(text)

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._masker.unmask(text)


# ---- per-conversation registry (memory only) ----

_current: ContextVar[str | None] = ContextVar("wisp_cloud_mask_conversation", default=None)
_registry: "OrderedDict[str, Masker]" = OrderedDict()


@contextmanager
def conversation(key: str | None) -> Iterator[None]:
    """Bind the conversation whose mapping requests made inside this block use."""
    token = _current.set(str(key) if key else None)
    try:
        yield
    finally:
        _current.reset(token)


def current_conversation() -> str | None:
    return _current.get()


def masker_for(key: str | None) -> Masker | None:
    """The mapping for a conversation key, created on first use; None without a key."""
    if not key:
        return None
    masker = _registry.get(key)
    if masker is None:
        masker = _registry[key] = Masker()
        while len(_registry) > _MAX_CONVERSATIONS:
            _registry.popitem(last=False)
    else:
        _registry.move_to_end(key)
    return masker


def forget(key: str | None = None) -> None:
    """Drop one conversation's mapping, or all of them."""
    if key is None:
        _registry.clear()
    else:
        _registry.pop(key, None)


def is_cloud(*, managed: bool, endpoint_name: str, base_url: str) -> bool:
    """True only for a remote provider: not the managed engine, not a user's own
    loopback provider, and not any loopback address."""
    if managed or endpoint_name == "local_provider":
        return False
    try:
        from service.config.endpoints import is_loopback
    except ImportError:          # a trimmed copy of the service (managed QA apps): plain host check
        from urllib.parse import urlsplit

        def is_loopback(url: str) -> bool:
            host = (urlsplit(url).hostname or "").lower()
            return host in {"localhost", "::1"} or host.startswith("127.")
    return not is_loopback(base_url or "")
