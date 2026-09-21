"""Explicit inference targets. Configuration contains references, never secrets."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit


class EndpointConfigurationError(ValueError):
    pass


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    credential_ref: str
    managed: bool = False
    readiness_timeout: float = 5.0
    provider: str = "omlx"
    api_prefix: str = "/v1"

    def api_key(self, *, purpose: str = "inference", node_id: str | None = None) -> str:
        if self.credential_ref == "local_omlx":
            if purpose != "inference" or self.name != "local" or not self.managed or not is_loopback(self.base_url):
                raise EndpointConfigurationError("Local credentials require the managed loopback endpoint")
            from service.config import omlx_api_key
            return omlx_api_key()
        prefix, _, name = self.credential_ref.partition(":")
        if prefix == "keychain":
            if purpose != "inference" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise EndpointConfigurationError("Invalid provider credential reference")
            from .provider_credentials import resolve_keychain
            return resolve_keychain(name, self.base_url)
        if prefix != "env" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EndpointConfigurationError(f"Invalid credential reference for endpoint {self.name}")
        if name == "WISP_LOCAL_OMLX_KEY" and (purpose != "inference" or self.name != "local" or not self.managed or not is_loopback(self.base_url)):
            raise EndpointConfigurationError("Local credentials require the managed loopback endpoint")
        from .credentials import resolve, verify_binding
        verify_binding(name, self.base_url, purpose=purpose, node_id=node_id)
        key = resolve(name)
        if not key:
            raise EndpointConfigurationError(f"Missing credential for endpoint {self.name}")
        return key


@dataclass(frozen=True)
class Target:
    role: str
    endpoint: Endpoint
    model: str
    revision: str = ""
    profile: str = ""
    context_window: int = 8000
    capabilities: tuple[str, ...] = ()
    dimensions: int = 0

    @property
    def identity(self) -> tuple:
        return (self.endpoint.name, self.endpoint.base_url, self.model,
                self.revision, self.profile, self.dimensions,
                self.endpoint.provider, self.endpoint.api_prefix)


def endpoint(name: str = "local") -> Endpoint:
    from service.config import models_config, omlx_base_url
    cfg = models_config().get("inference", {}).get("endpoints", {}).get(name)
    if cfg is None:
        if name != "local":
            raise EndpointConfigurationError(f"Unknown inference endpoint {name}")
        cfg = {"base_url": omlx_base_url(), "credential_ref": "local_omlx"}
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is not True:
        raise EndpointConfigurationError(f"Inference endpoint {name} is disabled or invalid")
    from service.inference.providers import provider
    profile = provider(cfg.get("provider", "omlx"))
    if any(field in cfg for field in ("api_key", "token", "password", "headers")):
        raise EndpointConfigurationError("Use credential references, never inline provider secrets")
    api_prefix = cfg.get("api_prefix", profile.api_prefix)
    if (not isinstance(api_prefix, str)
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", api_prefix)):
        raise EndpointConfigurationError("Invalid inference API prefix")
    if profile.name == "omlx" and api_prefix != "/v1":
        raise EndpointConfigurationError("The oMLX profile requires /v1")
    url = str(cfg.get("base_url", "")).rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise EndpointConfigurationError(f"Invalid base URL for endpoint {name}")
    managed = name == "local" and is_loopback(url)
    if managed and (profile.name != "omlx" or api_prefix != "/v1"):
        raise EndpointConfigurationError("Managed local inference requires the oMLX profile")
    if name == "local" and not managed:
        raise EndpointConfigurationError("The local endpoint must use loopback; configure a named remote endpoint")
    if not managed and parsed.scheme != "https":
        raise EndpointConfigurationError("Remote inference requires authenticated HTTPS")
    timeout = float(cfg.get("readiness_timeout", 5))
    if not 0 < timeout <= 30:
        raise EndpointConfigurationError("readiness_timeout must be between 0 and 30 seconds")
    ref = str(cfg.get("credential_ref", "local_omlx" if managed else ""))
    if not managed and ref == "local_omlx":
        raise EndpointConfigurationError("Remote endpoints cannot use local credentials")
    return Endpoint(name, url, ref, managed, timeout, profile.name, api_prefix)


def role_target(role: str) -> Target:
    from service.config import models_config, role_to_model, model_context_window
    cfg = models_config()
    binding = cfg.get("inference", {}).get("bindings", {}).get(role, {})
    if not isinstance(binding, dict):
        raise EndpointConfigurationError(f"Invalid binding for {role}")
    ep = endpoint(str(binding.get("endpoint", "local")))
    if ep.provider != "omlx" and role in {"embedding", "reranker"}:
        raise EndpointConfigurationError("This provider supports generation roles only")
    # Fast summaries, deterministic routing and native tool helpers remain local.
    if role in {"fast", "router"} and not ep.managed:
        raise EndpointConfigurationError(f"The {role} role must remain local")
    model = str(binding.get("model_id") or role_to_model(role))
    window = int(binding.get("context_window") or (model_context_window(model) if ep.managed else 8000))
    dims = int(binding.get("dimensions", 0))
    capabilities = binding.get("qualified_capabilities", [])
    if window < 512 or dims < 0 or not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise EndpointConfigurationError(f"Invalid model metadata for {role}")
    if not ep.managed and role == "embedding" and (not binding.get("revision") or dims <= 0):
        raise EndpointConfigurationError("Remote embeddings require revision and dimensions")
    return Target(role, ep, model, str(binding.get("revision") or ""),
                  str(binding.get("profile") or ""), window, tuple(capabilities), dims)
