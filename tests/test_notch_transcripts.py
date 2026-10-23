"""transcripts.html from a SYNTHETIC run directory: private, offline, inert.

Nothing here reads the user's corpus or results, starts the app or opens a
browser; the page is checked as text.
"""
from __future__ import annotations

import json
import os
import re

from scripts.notch_evaluation import transcripts

HOSTILE = '</script><img src=x onerror="alert(1)"><!-- SYNTHETIC'


def _run(tmp_path):
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    (run / "cases.json").write_text(json.dumps({"cases": [
        {"case_id": 1, "risk_tag": "R", "text": "SYNTHETIC one", "segments": ["SYNTHETIC one"]},
        {"case_id": 2, "risk_tag": "W", "text": HOSTILE, "segments": [HOSTILE]}]}))
    (run / "sections.json").write_text(json.dumps({"1": "Calendar", "2": "Notes"}))
    turn = {"prompt": HOSTILE, "final_text": "**Done.**\n- " + HOSTILE, "failure_class": "CONFIRMATION_DECLINED",
            "model": "SYNTH-MODEL", "route": {"role": "agent", "source": "rules"}, "latency_s": 1.5,
            "events": [{"type": "status", "text": "Checking your notes…"},
                       {"type": "tool_call", "id": "c1", "name": "create_note", "args": '{"title": "x"}',
                        "decision": "confirm"},
                       {"type": "confirm", "tool": "create_note", "reason": HOSTILE, "preview": "x"},
                       {"type": "tool_result", "id": "c1", "result": {"text": "R" * 9000}}],
            "activity": ["create_note — needs ok"], "confirmation": {"tool": "create_note", "declined": True}}
    lines = [{"case_id": 1, "risk_tag": "R", "failure_class": "TIMEOUT", "attempt": 1, "turns": []},
             {"case_id": 1, "risk_tag": "R", "failure_class": "OK", "attempt": 2, "model": "SYNTH-MODEL",
              "turns": [{"prompt": "SYNTHETIC one", "final_text": "SYNTHETIC answer", "failure_class": "OK"}]},
             {"case_id": 2, "risk_tag": "W", "failure_class": "CONFIRMATION_DECLINED", "attempt": 1,
              "turns": [turn]}]
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    return run


def _data(page: str) -> dict:
    raw = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1)
    return json.loads(raw)


def test_page_is_private_offline_and_carries_the_latest_attempt(tmp_path):
    run = _run(tmp_path)
    page_path = transcripts.render(run)
    assert oct(page_path.stat().st_mode & 0o777) == "0o600"
    page = page_path.read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', page)       # nothing external
    assert "default-src 'none'" in page
    data = _data(page)
    assert [c["id"] for c in data["cases"]] == [1, 2] and data["total_cases"] == 2
    first = data["cases"][0]
    assert first["class"] == "OK" and first["attempt"] == 2 and first["section"] == "Calendar"
    assert first["turns"][0]["answer"] == "SYNTHETIC answer"
    second = data["cases"][1]["turns"][0]
    assert second["answer"].startswith("**Done.**")                    # the complete answer
    kinds = [e["type"] for e in second["events"]]
    assert kinds == ["status", "tool_call", "confirm", "tool_result"]
    result = second["events"][3]["result"]
    assert len(result["text"]) == transcripts.RESULT_CHARS and result["clipped"] == 1000
    assert not [p for p in os.listdir(run) if p.startswith(".")]       # no staging file left


def test_hostile_case_text_stays_inert_data(tmp_path):
    page = transcripts.render(_run(tmp_path)).read_text(encoding="utf-8")
    assert HOSTILE not in page and "<img" not in page
    body = page.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in body and ">" not in body
    assert _data(page)["cases"][1]["turns"][0]["prompt"] == HOSTILE    # intact once parsed
    script = page.split("</script>", 1)[1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script


def test_an_empty_run_still_renders(tmp_path):
    run = tmp_path / "empty"
    run.mkdir(mode=0o700)
    data = _data(transcripts.render(run).read_text(encoding="utf-8"))
    assert data["cases"] == [] and data["total_cases"] == 0
