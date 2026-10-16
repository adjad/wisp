"""Dependency-free inference failures shared by primary and mini transports."""


class ModelLoadError(RuntimeError):
    """The configured model or its authenticated runtime is unavailable."""


class ModelMemoryLimitError(ModelLoadError):
    """Managed model admission was refused, without retaining server data."""

    reason = "model_memory_limit"

    def __init__(self) -> None:
        super().__init__(
            "The selected model could not load within the AI engine's current "
            "memory limit. Free some memory, then try again."
        )
