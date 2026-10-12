"""Optional LLM phrasers for the behavior engine (see ``engine.Phraser``).

The engine composes every nudge/profile sentence itself; a phraser may only
reword it. Any failure, timeout, empty answer, or answer that appears to add
facts returns ``None`` (or ``{}`` for ``categorize``) so the engine keeps its
code-composed text.

``OpenAIPhraser`` talks to any OpenAI-compatible ``/chat/completions``:

- On the GX10 brain server: native Ollama, ``http://127.0.0.1:11434`` +
  ``/v1`` (no token).
- On the Intel Mac (local engine, remote-only build): the GX10 gateway,
  ``https://6wjp.tail4cc204.ts.net`` + ``/v1`` with the gateway bearer token
  (pass a callable so the secret is read from Keychain per request).

The request body is restricted to ``{model, messages, max_tokens, stream,
temperature}`` so it also satisfies the remote-only gateway contract
(``service.inference.remote_policy.RemoteOnlyProfile.validate_payload``).

Boundary: imports only stdlib, ``httpx`` and ``service.behavior.*``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

import httpx

__all__ = ["OpenAIPhraser", "TemplatePhraser", "strip_think"]

_ALLOWED_KEYS = frozenset({"model", "messages", "max_tokens", "stream", "temperature"})
_MAX_FACTS_BYTES = 4000          # keeps us far below the gateway's 24000 aggregate budget
_MAX_RESPONSE_BYTES = 262144
_MAX_OUTPUT_CHARS = 320
_MAX_TITLES_PER_CALL = 40
_MAX_TITLE_CHARS = 120
# Phrasing is decoration: like the Mac WispPhraser (service/assistant/
# behavior_phraser.py: 4 calls / 60s), cap phrase() calls per instance so a burst
# of nudges can never queue minutes of generation behind the engine lock.
# categorize() is not capped: it runs once per mine() and its answers are cached.
PHRASE_MAX_CALLS_PER_WINDOW = 4
PHRASE_WINDOW_S = 60.0
_NO_THINK = re.compile(r"/no_think\b", re.IGNORECASE)
# Chatty preambles mean the model is talking to us, not the user: use the template.
_FILLER = re.compile(r"^\W*(?:sure|here['’]?s|here is|here you go|certainly|of course|absolutely|"
                     r"okay|ok|rewritten|notification)\b", re.IGNORECASE)

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_CAPWORD = re.compile(r"\b[A-Z][a-zA-Z'\-]+\b")
# Capitalized words the model may use without them being "new facts".
_ALLOWED_CAPWORDS = frozenset({"I", "I'm", "I'll", "You", "Your", "You're", "Wisp", "OK", "Ok"})

PHRASE_SYSTEM = (
    "You rewrite one short notification for a personal assistant app. "
    "Use ONLY the facts in the JSON you are given. Never add names, people, places, "
    "dates, times, numbers, reasons or advice that are not in the facts. "
    "Write at most 2 short sentences (under 40 words), warm and plain, addressed to the user as 'you'. "
    "Output only the rewritten text: no quotes, no preamble, no lists, no emoji."
)

CATEGORIZE_SYSTEM = (
    "You label calendar event titles. For each numbered title choose exactly one category "
    "from the allowed list. If unsure, use \"other\". Reply with ONE JSON object that maps the "
    "title number (as a string) to the category, for example {\"1\": \"fitness\", \"2\": \"meeting\"}. "
    "Output only the JSON object."
)


def strip_think(text: str) -> str:
    """Remove reasoning blocks. A ``<think>`` that never closes means the answer
    was truncated inside reasoning: nothing usable remains."""
    if not isinstance(text, str):
        return ""
    out = _THINK_BLOCK.sub("", text)
    # Some chat templates emit the opening tag in the prompt: keep only what follows
    # the last stray closing tag.
    parts = _THINK_CLOSE.split(out)
    out = parts[-1]
    m = _THINK_OPEN.search(out)
    if m:
        out = out[: m.start()]
    return out.strip()


def _clean_sentence(text: str) -> str:
    text = _NO_THINK.sub("", text)          # some models echo the control token
    text = text.strip().strip("`").strip()
    if _FILLER.match(text):
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    text = re.sub(r"\s+", " ", text)
    # At most two sentences.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", text) if s.strip()]
    text = " ".join(sentences[:2])
    if len(text) > _MAX_OUTPUT_CHARS:
        return ""
    return text


def _adds_facts(text: str, facts_text: str, kind: str) -> bool:
    """Conservative guard: reject numbers or capitalized names that are not in
    the facts. False positives only cost us the template fallback."""
    haystack = facts_text.lower() + " " + kind.lower().replace("_", " ")
    fact_numbers = {n.lstrip("0") or "0" for n in _NUMBER.findall(facts_text)}
    for n in _NUMBER.findall(text):
        if (n.lstrip("0") or "0") not in fact_numbers:
            return True
    # Words that begin a sentence are capitalized grammatically; skip those.
    sentence_starts = {m.end() for m in re.finditer(r"(?:^|[.!?]\s+)", text)}
    for m in _CAPWORD.finditer(text):
        word = m.group(0)
        if m.start() in sentence_starts or word in _ALLOWED_CAPWORDS:
            continue
        if word.lower() not in haystack:
            return True
    return False


class TemplatePhraser:
    """Explicit no-LLM phraser: the engine always uses its own text."""

    def phrase(self, kind: str, facts: dict) -> str | None:
        return None

    def categorize(self, titles: list[str], categories: tuple[str, ...]) -> dict[str, str]:
        return {}


class OpenAIPhraser:
    """Phraser over an OpenAI-compatible chat completions endpoint (sync httpx).

    ``token`` may be ``None`` (no auth, e.g. loopback Ollama), a string, or a
    zero-argument callable returning the token (read lazily per request, e.g.
    from Keychain). One request is in flight per instance; a concurrent call
    that cannot get the slot within ``timeout`` returns ``None``.
    """

    def __init__(self, base_url: str, api_prefix: str = "/v1", model: str = "",
                 token: str | Callable[[], str] | None = None, timeout: float = 8.0,
                 max_tokens: int = 160, *, temperature: float = 0.3, no_think: bool = True,
                 transport: httpx.BaseTransport | None = None,
                 max_phrase_calls: int = PHRASE_MAX_CALLS_PER_WINDOW,
                 phrase_window_s: float = PHRASE_WINDOW_S, clock: Callable[[], float] = time.monotonic,
                 ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("OpenAIPhraser requires an exact model id")
        if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens must be an int in 1..4096")
        if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", api_prefix or ""):
            raise ValueError("Invalid api_prefix")
        self.base_url = str(base_url).rstrip("/")
        self.api_prefix = api_prefix or ""
        self.model = model
        self.timeout = float(timeout)
        self.max_tokens = max_tokens
        self.temperature = float(temperature)
        # Qwen3-family models honour "/no_think"; without it a small max_tokens
        # budget can be spent entirely on reasoning and yield no answer.
        self.no_think = bool(no_think)
        self._token = token
        self._lock = threading.Lock()
        self._max_phrase_calls = int(max_phrase_calls)
        self._phrase_window_s = float(phrase_window_s)
        self._clock = clock
        self._phrase_calls: list[float] = []
        self._admit_lock = threading.Lock()
        self._client = httpx.Client(timeout=httpx.Timeout(self.timeout), transport=transport,
                                    follow_redirects=False, trust_env=False)

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.api_prefix}/chat/completions"

    def close(self) -> None:
        self._client.close()

    # ---- transport ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = self._token() if callable(self._token) else self._token
        if token:
            if not isinstance(token, str) or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("invalid token")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def build_payload(self, system: str, user: str, *, temperature: float | None = None,
                      max_tokens: int | None = None) -> dict:
        if self.no_think:
            user = f"{user}\n/no_think"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
            "temperature": float(self.temperature if temperature is None else temperature),
        }
        assert set(payload) <= _ALLOWED_KEYS
        return payload

    def _complete(self, payload: dict) -> str | None:
        if not self._lock.acquire(timeout=self.timeout):
            return None
        try:
            resp = self._client.post(self.url, json=payload, headers=self._headers())
            if resp.status_code != 200 or len(resp.content) > _MAX_RESPONSE_BYTES:
                return None
            data = resp.json()
            content = data["choices"][0]["message"].get("content")
            if not isinstance(content, str):
                return None
            text = strip_think(content)
            return text or None
        except Exception:  # noqa: BLE001 - any failure means "use the template"
            return None
        finally:
            self._lock.release()

    def _admit_phrase(self) -> bool:
        """Rate cap for phrase(): at most ``max_phrase_calls`` per window."""
        now = self._clock()
        with self._admit_lock:
            self._phrase_calls = [t for t in self._phrase_calls if now - t < self._phrase_window_s]
            if len(self._phrase_calls) >= self._max_phrase_calls:
                return False
            self._phrase_calls.append(now)
        return True

    # ---- Phraser protocol ---------------------------------------------------
    def phrase(self, kind: str, facts: dict) -> str | None:
        try:
            facts_text = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        if len(facts_text.encode("utf-8")) > _MAX_FACTS_BYTES:
            return None
        user = f"Notification kind: {kind}\nFacts (JSON): {facts_text}"
        if not self._admit_phrase():
            return None
        raw = self._complete(self.build_payload(PHRASE_SYSTEM, user))
        if not raw:
            return None
        text = _clean_sentence(raw)
        if not text or _adds_facts(text, facts_text, str(kind)):
            return None
        return text

    def categorize(self, titles: list[str], categories: tuple[str, ...]) -> dict[str, str]:
        allowed = tuple(c for c in categories if isinstance(c, str) and c)
        if not allowed:
            return {}
        unique: list[str] = []
        for t in titles or ():
            if isinstance(t, str) and t.strip() and t not in unique:
                unique.append(t)
        result: dict[str, str] = {}
        for start in range(0, len(unique), _MAX_TITLES_PER_CALL):
            batch = unique[start:start + _MAX_TITLES_PER_CALL]
            numbered = "\n".join(f"{i}. {t[:_MAX_TITLE_CHARS]}" for i, t in enumerate(batch, 1))
            user = (f"Allowed categories: {', '.join(allowed)}\n"
                    f"Titles:\n{numbered}")
            max_tokens = max(self.max_tokens, min(4096, 16 * len(batch) + 32))
            raw = self._complete(self.build_payload(CATEGORIZE_SYSTEM, user, temperature=0.0,
                                                    max_tokens=max_tokens))
            for title, cat in _parse_mapping(raw, batch, allowed).items():
                result[title] = cat
        return result


def _parse_mapping(raw: str | None, batch: list[str], allowed: tuple[str, ...]) -> dict[str, str]:
    if not raw:
        return {}
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    allowed_set = set(allowed)
    out: dict[str, str] = {}
    for key, value in obj.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        value = value.strip().lower()
        if value not in allowed_set:
            continue
        k = key.strip()
        title: str | None = None
        if k.isdigit() and 1 <= int(k) <= len(batch):
            title = batch[int(k) - 1]
        elif k in batch:
            title = k
        if title is not None:
            out[title] = value
    return out
