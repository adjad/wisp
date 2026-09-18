"""Dependency-free inference failures shared by primary and mini transports."""


class ModelLoadError(RuntimeError):
    """The configured model or its authenticated runtime is unavailable."""
