"""Reviewed wire profiles; these describe APIs, never select tools or models."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Provider:
    name: str
    api_prefix: str
    omlx_health: bool = False
    inventory_bytes: int = 8 * 1024 * 1024
    inventory_rows: int = 10000


PROVIDERS = MappingProxyType({
    "omlx": Provider("omlx", "/v1", omlx_health=True,
                     inventory_bytes=1024 * 1024, inventory_rows=1000),
    "openai-compatible": Provider("openai-compatible", "/v1"),
    "openrouter": Provider("openrouter", "/api/v1"),
    # Google's Gemini API, through its OpenAI-compatible surface. Pinned to Google's
    # own origin and prefix (service/config/endpoints.py): a key saved for it can
    # never be sent anywhere else.
    "gemini": Provider("gemini", "/v1beta/openai"),
})

# The exact origins of the two cloud providers the app offers. A remote-only
# profile stores no provider name, so its wire format is told from its origin.
GEMINI_ORIGIN = "https://generativelanguage.googleapis.com"
GEMINI_API_PREFIX = "/v1beta/openai"
OPENROUTER_ORIGIN = "https://openrouter.ai"


def wire_provider(provider_name: str, base_url: str) -> str:
    """The wire format a request to `base_url` speaks: "gemini", "openrouter", or the
    configured provider name. Decided by exact origin only; a look-alike host is just
    "openai-compatible"."""
    if provider_name in {"gemini", "openrouter"}:
        return provider_name
    if provider_name == "openai-compatible":
        origin = (base_url or "").rstrip("/")
        if origin == GEMINI_ORIGIN:
            return "gemini"
        if origin == OPENROUTER_ORIGIN:
            return "openrouter"
    return provider_name


def provider(name: str) -> Provider:
    from service.config.endpoints import EndpointConfigurationError
    try:
        return PROVIDERS[name]
    except (KeyError, TypeError):
        raise EndpointConfigurationError("Unknown inference provider") from None
