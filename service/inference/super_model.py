"""Fail-local Laya privacy policy for cloud-first Super Model routing."""
from __future__ import annotations

import asyncio
import html
import re
import unicodedata
from urllib.parse import unquote
import threading
from typing import Any


LAYA_MODEL_ID = "aac6fef/laya-multilingual-coreml"
LAYA_MODEL_REVISION = "8139e9089273319512c730218903784074133187"
# Ambiguous standalone requests retain the conservative fail-local threshold.
# Laya's computer-access score is noisy for explicitly scoped public tools;
# those routes still require low private-content and context-risk scores.
_CLOUD_RISK_CEILING = 0.05
_PUBLIC_CLOUD_TOOLS = frozenset({
    "air_quality", "get_stock_price", "get_weather", "rain_radar",
    "weather_alerts", "web_search",
})
_WEATHER_ROUTE_MENU = _PUBLIC_CLOUD_TOOLS | {"http_request", "web_fetch"}
_PAGE_SEPARATORS = str.maketrans({
    "。": ".", "．": ".", "｡": ".", "․": ".", "﹒": ".",
    "／": "/", "∕": "/", "⁄": "/",
})
_PAGE_PATH_RE = re.compile(
    r"\b(?:https?://|www\.|localhost\b)|\[[0-9a-f:]+\]|::|"
    r"\b(?:0x[0-9a-f]{7,8}|\d{8,10})\b|"
    r"\b(?:[0-9a-f]{1,4}:){2,}[0-9a-f]{1,4}\b", re.I)
_DOTTED_TOKEN_RE = re.compile(r"\b[\w-]+(?:\.[\w-]+)+\b")
_NON_PAGE_SLASH_RE = re.compile(r"\b(?:and/or|\d+/\d+)\b", re.I)


def _has_page_reference(prompt: str) -> bool:
    """Keep page-shaped input local even when the default router offers optional tools."""
    if any(unicodedata.category(char) == "Cf" for char in prompt):
        return True
    normalized = prompt
    for _ in range(8):
        decoded = html.unescape(unquote(normalized))
        if decoded == normalized:
            break
        normalized = decoded
    normalized = unicodedata.normalize("NFKC", normalized).translate(_PAGE_SEPARATORS)
    slash_scan = _NON_PAGE_SLASH_RE.sub("", normalized)
    if "/" in slash_scan or "\\" in slash_scan:
        return True
    # Fail local if a deeply nested encoding still hides punctuation.
    if re.search(r"%[0-9a-f]{2}|&#", normalized, re.I):
        return True
    if _PAGE_PATH_RE.search(normalized):
        return True
    return any(any(char.isalpha() for char in match.group())
               or match.group().count(".") >= 3
               for match in _DOTTED_TOKEN_RE.finditer(normalized))
_agent = None
_agent_lock = threading.Lock()
_loading = False
_load_failed = False
_warm_task: asyncio.Task | None = None

# These are non-negotiable policy checks rather than a semantic taxonomy.
# Everything nuanced is delegated to local Laya and uncertainty fails local.
_EXPLICIT_LOCAL_RE = re.compile(
    r"\buse\s+(?:only\s+)?(?:the\s+)?local(?:\s+model)?\b|"
    r"\blocal\s+only\b|"
    r"\b(?:keep|stay|process|answer|run|handle)\s+(?:(?:this|it)\s+)?"
    r"(?:entirely\s+|only\s+)?(?:local(?:ly)?|on[- ]device|"
    r"on\s+(?:my|this)\s+(?:mac|machine|computer|device))\b|"
    r"\b(?:do not|don't|never)\s+(?:use|send|share).{0,24}\bcloud\b",
    re.I,
)
_OBVIOUS_SECRET_RE = re.compile(
    r"\b(?:password|passcode|api[ _-]?key|access token|private key|secret key|"
    r"seed phrase|recovery code|social security|ssn)\b|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b|"
    r"(?:^|[^\w])(?:/(?:Users|home|tmp|private|var|etc|opt|Volumes)/|"
    r"~/|\./|\.\./|file://)",
    re.I,
)
_PERSONAL_SCOPE_RE = re.compile(
    r"\b(?:my|our)\s+(?:(?:latest|recent|unread|upcoming|private|personal)\s+){0,2}"
    r"(?:emails?|inbox|messages?|texts?|calendar|contacts?|documents?|files?|"
    r"notes?|photos?|accounts?|portfolio|clipboard|address)\b",
    re.I,
)

_QUESTIONS = {
    "private": {
        "type": "noul",
        "instructions": (
            "Does this request contain, reveal, or depend on personal, private, "
            "confidential, credential, health, financial, identity, or non-public information?"
        ),
    },
    "computer": {
        "type": "noul",
        "instructions": (
            "Does answering this request require reading or changing the user's "
            "computer, local application, account, file, or private data? "
            "A public web search or public weather or stock lookup is not "
            "access to the user's computer."
        ),
    },
    "context": {
        "type": "noul",
        "instructions": (
            "Does this request depend on earlier conversation, omitted context, "
            "or a previous answer to be understood correctly?"
        ),
    },
}


def _load_agent():
    global _agent, _loading, _load_failed
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        _loading = True
        try:
            import laya_coreml as laya
            # Super Model generation is remote, so the local GPU is available
            # for the richer 1,024-token Core ML classifier.
            _agent = laya.load(
                LAYA_MODEL_ID,
                revision=LAYA_MODEL_REVISION,
                compute_units="cpu_gpu",
            )
            _load_failed = False
            return _agent
        except Exception:  # noqa: BLE001 - every setup/runtime failure fails local
            _load_failed = True
            raise
        finally:
            _loading = False


async def warm_laya_classifier() -> None:
    try:
        await asyncio.to_thread(_load_agent)
    except Exception:  # noqa: BLE001 - status + fail-local routing carry the outcome
        return


def start_laya_warmup() -> None:
    global _warm_task
    if _warm_task is None or _warm_task.done():
        _warm_task = asyncio.create_task(warm_laya_classifier())


def laya_router_status() -> str:
    if _agent is not None:
        return "ready"
    if _loading:
        return "loading"
    if _load_failed:
        return "unavailable"
    return "not_loaded"


def _predict_with_laya(prompt: str) -> tuple[float, float, float]:
    result = _load_agent().predict(prompt, _QUESTIONS)
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("Invalid Laya response")
    values = []
    for name in ("private", "computer", "context"):
        answer = answers.get(name)
        probability = answer.get("noul") if isinstance(answer, dict) else None
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("Invalid Laya probability")
        value = float(probability)
        if not 0.0 <= value <= 1.0:
            raise ValueError("Invalid Laya probability")
        values.append(value)
    return values[0], values[1], values[2]


def cloud_default_standalone(decision: Any) -> bool:
    """An ambiguous default tool menu is not proof this turn needs a tool."""
    from service.tools.registry import REGISTRY

    selected = getattr(decision, "tool_subset", None) or ()
    if any(name == "use_skill" or getattr(REGISTRY.get(name), "category", None) == "skill_tool"
           for name in selected):
        return False
    return bool(
        getattr(decision, "route_source", "") == "default"
        and getattr(decision, "needs_tools", False)
        and not getattr(decision, "light_read", False)
        and not getattr(decision, "direct_calls", ())
        and not getattr(decision, "required_tool_groups", ())
        and not getattr(decision, "tool_argument_bindings", {})
        and not getattr(decision, "strict_read_limits", {})
        and not getattr(decision, "force_first_tool", None)
        and not getattr(decision, "reminder_action", "")
        and not getattr(decision, "conditional_tools", ())
    )


def _scoped_public_tool_subset(decision: Any) -> list[str] | None:
    """Public-only tools for a scoped route; never retain arbitrary fetchers."""
    if (getattr(decision, "route_source", "") == "default"
            or not getattr(decision, "needs_tools", False)):
        return None
    selected = list(getattr(decision, "tool_subset", None) or ())
    if not selected:
        return None
    bound = {name for name, _ in (getattr(decision, "direct_calls", ()) or ())}
    bound.update((getattr(decision, "tool_argument_bindings", {}) or {}).keys())
    for group in (getattr(decision, "required_tool_groups", ()) or ()):
        bound.update(group)
    forced = getattr(decision, "force_first_tool", None)
    if forced:
        bound.add(forced)
    if not bound <= _PUBLIC_CLOUD_TOOLS:
        return None
    if set(selected) <= _PUBLIC_CLOUD_TOOLS:
        return selected
    if (getattr(decision, "route_source", "") == "rules"
            and not bound and "get_weather" in selected
            and set(selected) <= _WEATHER_ROUTE_MENU):
        return [name for name in selected if name in _PUBLIC_CLOUD_TOOLS]
    return None


def prepare_cloud_standalone(decision: Any) -> None:
    """Remove optional unsafe tools before approved cloud generation."""
    if cloud_default_standalone(decision):
        decision.needs_tools = False
        decision.tool_subset = []
        decision.expect_tool_first = False
        decision.multi_round = False
    else:
        public_tools = _scoped_public_tool_subset(decision)
        if public_tools is not None:
            decision.tool_subset = public_tools


async def cloud_super_model_eligible(prompt: str, decision: Any) -> tuple[bool, str]:
    """Return whether one request may leave the Mac for cloud generation."""
    route_tools = set(getattr(decision, "tool_subset", None) or ())
    route_tools.update(name for name, _ in (getattr(decision, "direct_calls", ()) or ()))
    route_tools.update((getattr(decision, "tool_argument_bindings", {}) or {}).keys())
    for group in (getattr(decision, "required_tool_groups", ()) or ()):
        route_tools.update(group)
    forced = getattr(decision, "force_first_tool", None)
    if forced:
        route_tools.add(forced)
    public_tools = _scoped_public_tool_subset(decision)

    if getattr(decision, "light_read", False):
        return False, "local tool or private-data access required"
    if getattr(decision, "needs_tools", False):
        # Public, read-only retrieval can be selected and executed locally while
        # the configured cloud model performs the final synthesis.  An unscoped
        # tool route or any personal/effect/Mac tool remains entirely local.
        if not cloud_default_standalone(decision) and public_tools is None:
            return False, "local tool or private-data access required"
    elif route_tools and not route_tools <= _PUBLIC_CLOUD_TOOLS:
        return False, "local tool or private-data access required"
    if _has_page_reference(prompt):
        return False, "a page reference needs local-only retrieval"
    if _EXPLICIT_LOCAL_RE.search(prompt):
        return False, "the user requested local handling"
    if _OBVIOUS_SECRET_RE.search(prompt):
        return False, "an explicit local secret or path was detected"
    if _PERSONAL_SCOPE_RE.search(prompt):
        return False, "a personal-data reference needs local handling"
    # Leave room for the three typed questions inside Laya's 1,024-token
    # capacity. The runtime rejects over-capacity input; this early bound avoids
    # needless work and remains fail-local.
    if len(prompt) > 3_200:
        return False, "the request exceeds the local privacy router budget"
    try:
        risks = await asyncio.wait_for(
            asyncio.to_thread(_predict_with_laya, prompt), timeout=1.0)
    except Exception:  # noqa: BLE001 - unavailable, timeout, capacity, malformed result
        start_laya_warmup()
        return False, "the local Laya privacy router was unavailable or uncertain"
    labels = ("private content", "computer access", "conversation context")
    checked = (0, 2) if public_tools is not None else (0, 1, 2)
    highest = max(checked, key=risks.__getitem__)
    if risks[highest] >= _CLOUD_RISK_CEILING:
        return False, f"Laya detected {labels[highest]} risk"
    if public_tools is not None:
        return True, "Laya cleared private and context risk for scoped public tools"
    return True, "Laya classified this as standalone non-sensitive generation"
