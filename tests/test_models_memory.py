"""Quick settings' Model memory row reads `/models/memory`.

It reports only what the engine measured: a model with no measured footprint has
null bytes (never its estimate), and anything but a managed local oMLX engine, or a
failed status read, answers `available: false` instead of a guess.
"""
import asyncio
import types
from unittest.mock import AsyncMock

from service import main


def _client(monkeypatch, *, managed=True, omlx=True, status=None, error=None):
    fake = types.SimpleNamespace(managed=managed, provider=types.SimpleNamespace(omlx_health=omlx),
                                 status=AsyncMock(return_value=status, side_effect=error))
    monkeypatch.setattr(main, "client", fake, raising=False)
    return fake


def test_reports_measured_resident_models_only(monkeypatch):
    _client(monkeypatch, status={"models": [
        {"id": "Ling-3.0-tiny", "loaded": True, "actual_size": 4_200_000_000, "estimated_size": 9},
        {"id": "Ornith-1.0-9B", "loaded": False, "actual_size": None, "estimated_size": 6_000_000_000},
        {"id": "embedder", "loaded": True, "actual_size": None, "resident_estimated_size": 700_000_000},
        {"id": "weird", "loaded": True, "actual_size": True},
        {"loaded": True, "actual_size": 5},
        "not a row",
    ]})
    assert asyncio.run(main.models_memory()) == {"available": True, "models": [
        {"id": "Ling-3.0-tiny", "bytes": 4_200_000_000},
        {"id": "embedder", "bytes": None},
        {"id": "weird", "bytes": None},
    ]}


def test_nothing_loaded_is_available_and_empty(monkeypatch):
    _client(monkeypatch, status={"models": [{"id": "Ling-3.0-tiny", "loaded": False}]})
    assert asyncio.run(main.models_memory()) == {"available": True, "models": []}


def test_remote_or_non_omlx_engine_is_unavailable_without_a_read(monkeypatch):
    for managed, omlx in ((False, True), (True, False)):
        fake = _client(monkeypatch, managed=managed, omlx=omlx, status={"models": []})
        assert asyncio.run(main.models_memory()) == {"available": False, "models": []}
        fake.status.assert_not_awaited()


def test_failed_status_read_is_unavailable(monkeypatch):
    _client(monkeypatch, error=RuntimeError("engine down"))
    assert asyncio.run(main.models_memory()) == {"available": False, "models": []}
