"""HTTP client for a ``brain_server`` (``brain.mode: remote``).

``RemoteEngine`` exposes the same public methods as ``BehaviorEngine`` with
the same argument and return types, so the Mac runtime does not care which
one it holds. Transport failures and 5xx raise ``BrainUnavailable`` (the
runtime falls back to its local engine or skips the tick); ``profile_card``
instead returns the last good card (or ``""``) so the system prompt never
blocks on the GX10.

Boundary: imports only stdlib, ``httpx`` and ``service.behavior.*``.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import httpx

from service.behavior.types import Feedback, Nudge, Observation, Pattern, Prediction

__all__ = ["BrainUnavailable", "BrainAuthError", "RemoteEngine"]

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BrainUnavailable(RuntimeError):
    """The brain server could not be reached or failed (transport error / 5xx)."""


class BrainAuthError(BrainUnavailable):
    """401/403 from the brain server: wrong or missing token. Subclass of
    ``BrainUnavailable`` so callers that only fall back keep working."""


def _trim_lines(text: str, max_bytes: int) -> str:
    """Whole lines only, UTF-8 bytes <= max_bytes (same rule as the engine)."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    out: list[str] = []
    used = 0
    for line in text.splitlines():
        size = len(line.encode("utf-8")) + (1 if out else 0)
        if used + size > max_bytes:
            break
        out.append(line)
        used += size
    return "\n".join(out)


def _http_allowed(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class RemoteEngine:
    """Same public surface as ``BehaviorEngine``, over HTTP.

    ``token``: string or zero-argument callable (e.g. a Keychain lookup).
    ``client``: optional pre-built ``httpx.Client`` (tests pass FastAPI's
    ``TestClient``); otherwise one is created with ``transport``.
    ``cache_path``: optional file where the last profile card is persisted
    (mode 0600) so it survives restarts while the GX10 is unreachable.
    """

    def __init__(self, base_url: str, token: str | Callable[[], str], timeout: float = 5.0, *,
                 slow_timeout: float = 60.0, cache_path: str | Path | None = None,
                 client: httpx.Client | None = None,
                 transport: httpx.BaseTransport | None = None,
                 allow_insecure_http: bool = False) -> None:
        url = str(base_url).rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("RemoteEngine needs an http(s) origin without credentials")
        if parts.scheme == "http" and not (allow_insecure_http or _http_allowed(url)):
            raise ValueError("RemoteEngine requires https for non-loopback hosts")
        self.base_url = url
        self.timeout = float(timeout)
        self.slow_timeout = float(slow_timeout)
        self._token = token
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache_lock = threading.Lock()
        self._cached_card: str | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(transport=transport, follow_redirects=False,
                                              trust_env=False)
        if self._cache_path and self._cache_path.is_file():
            try:
                self._cached_card = self._cache_path.read_text("utf-8")
            except OSError:
                self._cached_card = None

    # ---- plumbing -------------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "RemoteEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token() if callable(self._token) else self._token
        except Exception as exc:  # noqa: BLE001 - credential store failure
            raise BrainAuthError("brain token unavailable") from exc
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise BrainAuthError("brain token missing or invalid")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: Any = None, slow: bool = False) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._client.request(
                method, self.base_url + path, params=clean or None,
                content=None if body is None else json.dumps(body, allow_nan=False).encode("utf-8"),
                headers={**self._headers(),
                         **({"Content-Type": "application/json"} if body is not None else {})},
                timeout=self.slow_timeout if slow else self.timeout,
            )
        except httpx.HTTPError as exc:
            raise BrainUnavailable(f"brain server unreachable: {type(exc).__name__}") from exc
        status = resp.status_code
        if status in (401, 403):
            raise BrainAuthError(f"brain server rejected credentials ({status})")
        if status >= 500 or status in (408, 429):
            raise BrainUnavailable(f"brain server error {status}")
        if 300 <= status < 400:
            # Never follow: the bearer token must stay on the configured origin.
            raise BrainUnavailable(f"brain server answered with a redirect ({status})")
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise BrainUnavailable("brain server response too large")
        try:
            data = resp.json() if resp.content else None
        except ValueError as exc:
            raise BrainUnavailable("brain server returned invalid JSON") from exc
        if status == 404:
            raise KeyError(_detail(data) or path)
        if status >= 400:
            raise ValueError(_detail(data) or f"brain server rejected request ({status})")
        return data

    # ---- health ---------------------------------------------------------------
    def healthy(self) -> bool:
        try:
            resp = self._client.get(self.base_url + "/healthz", timeout=self.timeout)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ---- ingest ---------------------------------------------------------------
    def observe(self, observations: Iterable[Observation]) -> int:
        rows = [o.to_dict() for o in observations]
        data = self._request("POST", "/v1/observe", body={"observations": rows})
        return int(data["count"])

    def observe_snapshot(self, source: str, observations: Iterable[Observation], *,
                         window_start: float, window_end: float, taken_at: float) -> dict:
        body = {"source": source, "observations": [o.to_dict() for o in observations],
                "window_start": window_start, "window_end": window_end, "taken_at": taken_at}
        return dict(self._request("POST", "/v1/snapshot", body=body, slow=True))

    # ---- learning -------------------------------------------------------------
    def mine(self, now: float | None = None) -> list[Pattern]:
        data = self._request("POST", "/v1/mine", body={"now": now}, slow=True)
        return [Pattern.from_dict(p) for p in data["patterns"]]

    def patterns(self, *, status: str | None = None, kind: str | None = None) -> list[Pattern]:
        data = self._request("GET", "/v1/patterns", params={"status": status, "kind": kind})
        return [Pattern.from_dict(p) for p in data["patterns"]]

    def set_pattern_status(self, pattern_id: str, status: str) -> Pattern:
        data = self._request("POST", f"/v1/patterns/{quote(str(pattern_id), safe='')}/status",
                             body={"status": status})
        return Pattern.from_dict(data["pattern"])

    # ---- predicting / acting --------------------------------------------------
    def predict(self, now: float | None = None, horizon_s: float | None = None) -> list[Prediction]:
        # None -> omitted -> the engine's default horizon (>= 24h, through end of tomorrow).
        data = self._request("GET", "/v1/predict", params={"now": now, "horizon_s": horizon_s})
        return [Prediction.from_dict(p) for p in data["predictions"]]

    def generate_nudges(self, now: float | None = None) -> list[Nudge]:
        data = self._request("POST", "/v1/nudges/generate", body={"now": now}, slow=True)
        return [Nudge.from_dict(n) for n in data["nudges"]]

    def nudges_since(self, cursor: int = 0, limit: int = 50) -> tuple[list[Nudge], int]:
        data = self._request("GET", "/v1/nudges", params={"cursor": cursor, "limit": limit})
        return [Nudge.from_dict(n) for n in data["nudges"]], int(data["cursor"])

    def feedback(self, fb: Feedback) -> None:
        self._request("POST", "/v1/feedback", body=fb.to_dict())

    # ---- personal context -----------------------------------------------------
    def profile_card(self, now: float | None = None, max_bytes: int = 700) -> str:
        try:
            data = self._request("GET", "/v1/profile", params={"now": now, "max_bytes": max_bytes})
            text = str(data.get("text", ""))
        except BrainUnavailable:
            with self._cache_lock:
                cached = self._cached_card
            return _trim_lines(cached or "", max_bytes)
        self._remember_card(text)
        return text

    def describe(self, subject: str | None = None, now: float | None = None) -> dict:
        return dict(self._request("GET", "/v1/describe", params={"subject": subject, "now": now}))

    def status(self) -> dict:
        return dict(self._request("GET", "/v1/status"))

    # ---- cache ----------------------------------------------------------------
    def _remember_card(self, text: str) -> None:
        with self._cache_lock:
            self._cached_card = text
            if not self._cache_path:
                return
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, self._cache_path)
            except OSError:
                pass


def _detail(data: Any) -> str:
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
    return ""
