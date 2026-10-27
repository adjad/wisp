"""Dependency-free inference failures shared by primary and mini transports."""


class ModelLoadError(RuntimeError):
    """The configured model or its authenticated runtime is unavailable."""


class EngineGuardUnavailableError(ModelLoadError):
    reason = "engine_guard_unavailable"

    def __init__(self) -> None:
        super().__init__("The AI engine's memory guard could not be verified. Wait a moment, then try again.")


class ModelMemoryLimitError(ModelLoadError):
    """Managed model admission was refused, without retaining server data.

    The engine's memory guard refuses a load that would leave too little
    memory free, so other open apps can cause it (service/engine/manager.py:
    "balanced" refused Gemma on a 24 GB Mac with other apps open). A model
    that never fits is the case quitting apps cannot fix, hence both steps."""

    reason = "model_memory_limit"

    def __init__(self) -> None:
        super().__init__(
            "Wisp's model couldn't load in the memory this Mac has free right now. "
            "Quit some apps, then try again. If it keeps happening, choose a smaller "
            "model in Wisp's Settings → Models."
        )


SETUP_NEEDED_MESSAGE = ("Wisp's model isn't downloaded yet. To finish setup, open Wisp's "
                        "Settings → Models and choose Setup guide…")


class ModelNotDownloadedError(ModelLoadError):
    """The engine does not have the model a role asks for (it was never
    downloaded, or the download is still running). `reason` doubles as the
    /agent error code, so the app can point the user at setup."""

    reason = "setup_needed"

    def __init__(self, progress: float | None = None) -> None:
        if progress is None:
            message = SETUP_NEEDED_MESSAGE
        else:
            message = (f"Wisp's model is still downloading ({int(progress)}% done). "
                       "Ask again when it finishes. To watch it, open Wisp's "
                       "Settings → Models and choose Setup guide…")
        super().__init__(message)
        self.progress = progress
