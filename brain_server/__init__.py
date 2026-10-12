"""Wisp brain server: hosts one ``service.behavior.BehaviorEngine`` behind a
small bearer-authenticated HTTP API so the GX10 can run the behavior engine
for a Mac in ``brain.mode: remote``. See README.md."""
from brain_server.app import create_app

__all__ = ["create_app"]
__version__ = "0.1.0"
