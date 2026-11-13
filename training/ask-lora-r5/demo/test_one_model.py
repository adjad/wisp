"""Offline: one_model.only leaves at most the kept model loaded, and asks for nothing else."""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import one_model  # noqa: E402


def test_only_unloads_everything_but_the_kept_model(monkeypatch):
    loaded = {"base", "tuned", "other"}
    seen = []

    def fake(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        seen.append(url)
        if url.endswith("/v1/models/status"):
            models = [{"id": m, "loaded": m in loaded} for m in ("base", "tuned", "other", "idle")]
            return io.BytesIO(json.dumps({"models": models, "current_model_memory": 5e9}).encode())
        mid = url.split("/models/")[1].split("/unload")[0]
        loaded.discard(mid)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(one_model.urllib.request, "urlopen", fake)
    assert one_model.only("tuned", log=lambda s: None) == ["tuned"]
    assert one_model.only(None, log=lambda s: None) == []
    assert all(u.startswith("http://127.0.0.1:8000/") for u in seen)
    assert not any("idle" in u for u in seen)          # never touches a model that isn't loaded
