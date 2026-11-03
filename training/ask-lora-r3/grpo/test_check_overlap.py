"""Tests for check_overlap.py (RL-SCOPE 4.3, C15); pure python with synthetic manifests and files.
Run: python -m pytest -q test_check_overlap.py"""
import hashlib
import json
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_overlap as C  # noqa: E402

GATE = ["what is on my calendar next friday afternoon", "wipe my reminders for today please", "forward the landlord email to my accountant"]


def shingles(prompts):
    items = []
    for p in prompts:
        t = C._norm_words(p)
        items.append({"ntok": len(t), "full": C._h(t), "sh": {str(n): sorted({C._h(t[i:i + n]) for i in range(len(t) - n + 1)})
                                                                    for n in range(1, 7) if len(t) >= n}})
    return {"n_prompts": len(items), "items": items}


def manifest(**kw):
    m = {"sets": {"gate": {"prompts": GATE, "threads": ["eval-thread-1"]}}, "shingles": shingles(GATE), "fact_values": ["acme holdings llc"]}
    m.update(kw)
    return m


def rec(id_, users, conv="", thread=None, **kw):
    return {"id": id_, "modality": kw.pop("modality", "text"), "users": users, "conv": conv, "thread": thread, **kw}


def write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def run(tmp_path, rows, man=None, extra=(), name="rows.jsonl"):
    inp = write(tmp_path, name, rows)
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(man if man is not None else manifest()))
    rc = C.main(["--inputs", str(inp), "--manifest", str(mp), "--report", str(tmp_path / "report.json"), *extra])
    return rc, json.loads((tmp_path / "report.json").read_text())


def checks(rep):
    return sorted({h["check"] for h in rep["hits"]})


CLEAN = [rec("a#0", ["remind me to water the plants"], "remind me to water the plants"),
         rec("b#0", ["how long does rice take to cook", "and for brown rice"], "how long does rice take to cook\nabout 15 minutes")]


def test_clean_rows_exit_0_with_a_deterministic_report(tmp_path):
    rc, rep = run(tmp_path, CLEAN)
    assert rc == 0 and rep["result"] == "clean" and rep["hit_count"] == 0 and rep["rows"] == 2 and rep["skipped"] == []
    first = (tmp_path / "report.json").read_bytes()
    rc2, _ = run(tmp_path, CLEAN)
    assert rc2 == 0 and (tmp_path / "report.json").read_bytes() == first                    # no timestamps: the sha is reproducible
    assert rep["manifest_sha256"] and rep["excluded_sets"]["gate"]["prompts"] == 3 and rep["shingle_n_prompts"] == 3


def test_every_user_turn_is_checked_not_only_the_last(tmp_path):
    rows = [rec("a#0", ["wipe my reminders for today please", "and then what"], "x")]
    rc, rep = run(tmp_path, rows)
    assert rc == 1 and checks(rep) == ["fold"] and rep["hits"][0]["set"] == "gate"
    # weekdays and numbers fold to placeholders: the same template for another day is the same question
    rc, rep = run(tmp_path, [rec("b#0", ["what is on my calendar next monday afternoon"])])
    assert rc == 1 and checks(rep) == ["fold"]


def test_thread_hit(tmp_path):
    rc, rep = run(tmp_path, [rec("a#0", ["completely fresh words about gardening"], thread="eval-thread-1")])
    assert rc == 1 and checks(rep) == ["thread"] and rep["hits"][0]["thread"] == "eval-thread-1"
    assert run(tmp_path, [rec("a#0", ["completely fresh words about gardening"], thread="other")])[0] == 0


def test_trigram_hit_and_review_with_signoff(tmp_path):
    close = "what is on my calendar next friday afternoon please"          # Jaccard well above 0.7 but a different fold
    rc, rep = run(tmp_path, [rec("a#0", [close])])
    assert rc == 1 and "trigram" in checks(rep) and rep["hits"][0]["jaccard"] >= 0.7
    # 0.5 to 0.7 goes to review: exit 2 until a person signs it off
    mid = "what is on the calendar for friday afternoon"
    rc, rep = run(tmp_path, [rec("a#0", [mid])])
    assert rc == 2 and rep["result"] == "review" and rep["review_unsigned"] == 1 and rep["hit_count"] == 0
    r = next(x for x in rep["review"] if x["check"] == "trigram")
    assert 0.5 <= r["jaccard"] < 0.7
    sign = tmp_path / "signoff.json"
    sign.write_text(json.dumps({"signed": [r["key"]]}))
    rc, rep = run(tmp_path, [rec("a#0", [mid])], extra=("--signoff", str(sign)))
    assert rc == 0 and rep["review_unsigned"] == 0 and rep["review"][0]["signed"] is True


def test_shingles_full_prompt_hit_six_gram_review_and_a_silent_omission(tmp_path):
    # full-prompt hash: the same words with different punctuation and case, in a set the manifest does not list verbatim
    man = manifest(sets={})
    rc, rep = run(tmp_path, [rec("a#0", ["Forward the landlord email, to my accountant!"])], man)
    assert rc == 1 and checks(rep) == ["shingle"] and rep["hits"][0]["why"] == "full-prompt hash"
    rc, rep = run(tmp_path, [rec("b#0", ["please forward the landlord email to my boss today"])], man)
    assert rc == 2 and rep["review"][0]["check"] == "shingle"                                # a shared 6-gram: review
    rc, rep = run(tmp_path, CLEAN, manifest(min_shingle_prompts=4))
    assert rc == 1 and "left out" in rep["hits"][0]["why"]                                    # heldout_safety silently missing
    rc, rep = run(tmp_path, CLEAN, manifest(shingles=None))
    assert rc == 3 and any("shingle" in s for s in rep["skipped"])
    assert run(tmp_path, CLEAN, manifest(shingles=None), extra=("--allow-skips",))[0] == 0


def test_fact_values_in_the_conversation_text(tmp_path):
    rc, rep = run(tmp_path, [rec("a#0", ["fresh words here"], "the invoice from ACME Holdings LLC is due")])
    assert rc == 1 and checks(rep) == ["facts"] and rep["hits"][0]["values"] == ["acme holdings llc"]
    assert run(tmp_path, [rec("a#0", ["fresh words here"], "the invoice from Acme Holdings is due")])[0] == 0      # whole value only
    rc, rep = run(tmp_path, CLEAN, extra=("--facts", "skip"))
    assert rc == 3 and any("facts" in s for s in rep["skipped"])


def test_builder_format_inputs_check_history_turns_and_use_the_sibling_meta(tmp_path):
    ex = {"messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "wipe my reminders for today please"},
                       {"role": "assistant", "content": "Done."}, {"role": "user", "content": "and next week"},
                       {"role": "assistant", "content": "Sure."}], "tools": []}
    train = write(tmp_path, "train.jsonl", [ex])
    write(tmp_path, "train.meta.jsonl", [{"id": "chat-001-s0", "thread": "eval-thread-1"}])
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest()))
    rc = C.main(["--inputs", str(train), "--manifest", str(mp), "--report", str(tmp_path / "r.json")])
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rc == 1 and checks(rep) == ["fold", "thread"] and rep["hits"][0]["row"] == "chat-001-s0"


def test_rows_without_user_turns_cannot_be_checked(tmp_path):
    rc, rep = run(tmp_path, [{"id": "x#0", "family": "f", "modality": "text", "prompt": "<rendered>", "label": "{}"}])
    assert rc == 1 and checks(rep) == ["unchecked"] and rep["unchecked_rows"] == 1


def test_authoring_time_against_existing_training_sources(tmp_path):
    train = write(tmp_path, "train.jsonl", [{"messages": [{"role": "user", "content": "what time does the pharmacy close"},
                                                          {"role": "assistant", "content": "Nine."}], "tools": []}])
    write(tmp_path, "train.meta.jsonl", [{"id": "t-0-s0", "thread": "old-thread"}])
    new = write(tmp_path, "new.jsonl", [rec("n#0", ["what time does the pharmacy close tonight"], thread="fresh"),
                                        rec("n#1", ["a question about volcanoes"], thread="old-thread")])
    rc = C.main(["--inputs", str(new), "--against", f"r3={train}", "--report", str(tmp_path / "r.json"), "--allow-skips"])
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rc == 1 and checks(rep) == ["thread", "trigram"] and set(rep["excluded_sets"]) == {"r3"}


def test_forbidden_ids_and_the_record_in_the_stats(tmp_path):
    man = manifest(forbidden_ids=["pr_screen_photo_01"])
    rows = CLEAN + [rec("pr_screen_photo_01#0", [], modality="vision", images=[], target="x")]
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({"rows": 3}))
    rc, rep = run(tmp_path, rows, man, extra=("--stats", str(stats)))
    assert rc == 1 and checks(rep) == ["forbidden_id"]
    st = json.loads(stats.read_text())
    assert st["rows"] == 3 and st["overlap"]["report_sha256"] == hashlib.sha256((tmp_path / "report.json").read_bytes()).hexdigest()
    assert st["overlap"]["excluded_sets"]["gate"] == rep["excluded_sets"]["gate"]["sha256"] and st["overlap"]["result"] == "hits"
    assert st["overlap"]["manifest_sha256"] == rep["manifest_sha256"] and list(st["overlap"]["inputs"]) == [str(tmp_path / "rows.jsonl")]


def png(path, seed, size=64, flat=False, scale=1, level=120):
    import numpy as np
    from PIL import Image
    rng = np.random.RandomState(seed)
    base = np.full((size, size), level, dtype="uint8") if flat else rng.randint(0, 255, (size // 8, size // 8)).astype("uint8")
    im = Image.fromarray(base).resize((size * scale, size * scale), Image.NEAREST if not flat else Image.BILINEAR).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def test_vision_sha256_dhash_flat_images_and_literals(tmp_path):
    pytest.importorskip("PIL")
    gate = tmp_path / "gate"
    png(gate / "g1.png", 1)
    png(gate / "g2.png", 2, flat=True)
    entries = C.gate_image_hashes(gate)
    assert {e["name"] for e in entries} == {"g1.png", "g2.png"} and next(e for e in entries if e["name"] == "g2.png")["flat"] is True
    man = manifest(gate_images=entries, image_literals=["Cacio e pepe", "ok"], image_literal_allowlist=[])
    media = tmp_path / "mm"
    (media / "vision").mkdir(parents=True)
    png(media / "vision" / "same.png", 1)                       # byte-identical to the gate image
    (media / "vision" / "same.png").write_bytes((gate / "g1.png").read_bytes())
    png(media / "vision" / "near.png", 1, scale=2)               # the same picture re-rendered at twice the size: sha differs, dHash close
    png(media / "vision" / "other.png", 99)
    png(media / "vision" / "flat.png", 5, flat=True, level=60)             # flat: excluded from the dHash comparison
    rows = [rec("v1#0", [], modality="vision", images=["vision/same.png"], target="a sign"),
            rec("v2#0", [], modality="vision", images=["vision/near.png"], target="a sign"),
            rec("v3#0", [], modality="vision", images=["vision/other.png"], target="Cacio e pepe, 12 euro"),
            rec("v4#0", [], modality="vision", images=["vision/flat.png"], target="nothing")]
    rc, rep = run(tmp_path, rows, man, extra=("--media-root", str(media)))
    got = {(h["row"], h["check"]) for h in rep["hits"]}
    assert rc == 1 and ("v1#0", "image_sha256") in got and ("v2#0", "image_dhash") in got and ("v3#0", "image_literal") in got
    assert not any(h["row"] in ("v4#0",) for h in rep["hits"]) and not any(h["row"] == "v3#0" and h["check"].startswith("image_") and h["check"] != "image_literal"
                                                                              for h in rep["hits"])
    assert next(h for h in rep["hits"] if h["check"] == "image_dhash")["hamming"] <= C.DHASH_HAMMING
    # an allowlisted literal is reviewed and accepted
    man["image_literal_allowlist"] = ["cacio e pepe"]
    rc, rep = run(tmp_path, rows[2:], man, extra=("--media-root", str(media)))
    assert rc == 0
    # a missing file is a skipped check, not a pass
    rc, rep = run(tmp_path, [rec("v5#0", [], modality="vision", images=["vision/ghost.png"], target="x")], man, extra=("--media-root", str(media)))
    assert rc == 3 and any("ghost.png" in s for s in rep["skipped"])


def test_audio_transcript_and_pcm_hits(tmp_path):
    media = tmp_path / "mm" / "audio"
    media.mkdir(parents=True)
    wav = media / "c.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x02" * 800)
    man = manifest(audio={"truth": ["call mom at five thirty tomorrow afternoon"], "pcm_sha256": [C.pcm_sha256(wav)]})
    rows = [rec("a1#0", [], modality="audio", audio="audio/c.wav", reference="a sentence about nothing at all"),
            rec("a2#0", [], modality="audio", audio="audio/none.wav", reference="call mom at five thirty tomorrow afternoon please")]
    rc, rep = run(tmp_path, rows, man, extra=("--media-root", str(tmp_path / "mm")))
    got = {(h["row"], h["check"]) for h in rep["hits"]}
    assert rc == 1 and got == {("a1#0", "audio_pcm"), ("a2#0", "audio_transcript")}


def test_the_export_sidecar_is_what_the_checker_reads(tmp_path):
    """export_prompts writes {id, modality, users, conv, thread, folds}; run_checks reads exactly that shape."""
    import export_prompts as X
    line = X._sidecar_line({"id": "fam-001-s0", "copy": 2, "family": "fam", "modality": "text", "_users": ["hello there"], "_conv": "hello there",
                            "_thread": "t", "_folds": ["hello there"], "label": {}})
    r = json.loads(line)
    assert r == {"id": "fam-001-s0#2", "family": "fam", "modality": "text", "users": ["hello there"], "conv": "hello there", "thread": "t",
                 "folds": ["hello there"]}
    p = write(tmp_path, "s.jsonl", [r])
    assert C.read_records(p)[0]["users"] == ["hello there"] and C.read_records(p)[0]["thread"] == "t"


def test_an_export_file_is_read_through_its_sidecar(tmp_path):
    exp = write(tmp_path, "grpo_prompts.jsonl", [{"id": "x#0", "family": "f", "modality": "text", "prompt": "<rendered>", "label": "{}"}])
    write(tmp_path, "grpo_prompts.overlap.jsonl", [rec("x#0", ["wipe my reminders for today please"], family="f")])
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest()))
    rc = C.main(["--inputs", str(exp), "--manifest", str(mp), "--report", str(tmp_path / "r.json")])
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rc == 1 and checks(rep) == ["fold"] and list(rep["inputs"]) == [str(exp)] and rep["inputs"][str(exp)]["rows"] == 1
    # the image-hash helper prints manifest entries
    pytest.importorskip("PIL")
    png(tmp_path / "g" / "a.png", 3)
    out = C.gate_image_hashes(tmp_path / "g")
    assert out[0]["name"] == "a.png" and len(out[0]["sha256"]) == 64 and len(out[0]["dhash"]) == 64
