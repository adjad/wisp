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
})


def provider(name: str) -> Provider:
    from service.config.endpoints import EndpointConfigurationError
    try:
        return PROVIDERS[name]
    except (KeyError, TypeError):
        raise EndpointConfigurationError("Unknown inference provider") from None
