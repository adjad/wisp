"""finish_r5.py: dropped rows leave every split and stay aligned; a surviving placeholder is caught."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import finish_r5 as F  # noqa: E402


def _write(d, ids, content="ok"):
    for s, meta in F.SPLITS.items():
        (d / f"{s}.jsonl").write_text("".join(json.dumps({"messages": [{"role": "assistant", "content": content}]}) + "\n" for _ in ids))
        (d / meta).write_text("".join(json.dumps({"id": i}) + "\n" for i in ids))


def test_drop_keeps_alignment(tmp_path):
    _write(tmp_path, ["a", "b", "c"])
    assert F.drop_rows(tmp_path, {"b"}) == {s: 1 for s in F.SPLITS}
    for s, meta in F.SPLITS.items():
        assert [json.loads(m)["id"] for m in open(tmp_path / meta)] == ["a", "c"]
        assert sum(1 for _ in open(tmp_path / f"{s}.jsonl")) == 2


def test_placeholder_found(tmp_path):
    _write(tmp_path, ["a"], content=F.PLACEHOLDER)
    assert F.placeholders_left(tmp_path) == ["a"] * len(F.SPLITS)


def test_placeholder_matches_builder():
    builder = F.TRAINING / "ask-lora-r2" / "builder" / "scen_r5_debuglog.py"
    assert f'BASE_WRITES = "{F.PLACEHOLDER}"' in builder.read_text()
