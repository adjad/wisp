"""Explicit inference targets. Configuration contains references, never secrets."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
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

    def api_key(self) -> str:
        if self.credential_ref == "local_omlx":
            if not self.managed or not is_loopback(self.base_url):
                raise EndpointConfigurationError("Local credentials require the managed loopback endpoint")
            from service.config import omlx_api_key
            return omlx_api_key()
        prefix, _, name = self.credential_ref.partition(":")
        if prefix != "env" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EndpointConfigurationError(f"Invalid credential reference for endpoint {self.name}")
        key = os.environ.get(name, "").strip()
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
                self.revision, self.profile, self.dimensions)


def endpoint(name: str = "local") -> Endpoint:
    from service.config import models_config, omlx_base_url
    cfg = models_config().get("inference", {}).get("endpoints", {}).get(name)
    if cfg is None:
        if name != "local":
            raise EndpointConfigurationError(f"Unknown inference endpoint {name}")
        cfg = {"base_url": omlx_base_url(), "credential_ref": "local_omlx"}
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is not True:
        raise EndpointConfigurationError(f"Inference endpoint {name} is disabled or invalid")
    url = str(cfg.get("base_url", "")).rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise EndpointConfigurationError(f"Invalid base URL for endpoint {name}")
    managed = name == "local" and is_loopback(url)
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
    return Endpoint(name, url, ref, managed, timeout)


def role_target(role: str) -> Target:
    from service.config import models_config, role_to_model, model_context_window
    cfg = models_config()
    binding = cfg.get("inference", {}).get("bindings", {}).get(role, {})
    if not isinstance(binding, dict):
        raise EndpointConfigurationError(f"Invalid binding for {role}")
    ep = endpoint(str(binding.get("endpoint", "local")))
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
