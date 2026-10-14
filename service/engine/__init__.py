"""Wisp's own bundled oMLX engine: a private, isolated instance Wisp starts and stops.

It never shares a port, settings folder, cache, HOME or process with a user's
installed oMLX.app (port 8000, ~/.omlx). See docs/plans/2026-10-13-bundled-omlx-engine.md.
"""
from .manager import (BUNDLED_PORT, EngineError, EngineLayout, EngineManager,
                      RESERVED_PORTS, engine_settings)

__all__ = ["BUNDLED_PORT", "EngineError", "EngineLayout", "EngineManager",
           "RESERVED_PORTS", "engine_settings"]
