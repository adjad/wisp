#!/usr/bin/env python3
"""Audio check: service/voice.py's own request (its _PROMPT, input_audio wav, temperature 0, thinking off,
max_tokens 240, cache-store off) sent straight to oMLX, one model resident at a time; Wisp's audio gate checked.

    run_audio.py run   --repo <main checkout> --models A,B --out DIR
    run_audio.py score DIR/answers.jsonl
"""
import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8000")
MODEL_ROOT = Path(os.environ.get("OMLX_MODEL_ROOT", str(Path.home() / "Desktop/OMLX_Model_Files")))
MODEL_DIRS = {"gemma-4-E4B-it-oQ4e-mtp": "Jundot/gemma-4-E4B-it-oQ4e-mtp",
              "NVIDIA-Nemotron-3.5-Bolt-E4B-MLX-oQ4e-mtp": "UraionLabs/NVIDIA-Nemotron-3.5-Bolt-E4B-MLX-oQ4e-mtp"}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


def post(path, body=b"", headers=None, timeout=300):
    req = urllib.request.Request(BASE + path, body, method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


sys.path.insert(0, str(HERE.parents[1] / "mmeval"))
import scoring as S  # noqa: E402  Unicode-aware WER/CER shared with the GPU eval (pure python)


def wer(ref, hyp, norm=True):
    return S.wer(ref, hyp, norm)


def cmd_run(a):
    os.environ.setdefault("MOE_DIR", tempfile.mkdtemp(prefix="audioeval-moe-"))
    os.environ.setdefault("WISP_HOME", os.environ["MOE_DIR"])
    sys.path.insert(0, a.repo)
    from service import voice
    truth = json.loads((HERE / "truth.json").read_text())
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    fh = open(out / "answers.jsonl", "a")
    for model in a.models.split(","):
        st = {m["id"]: m for m in get("/v1/models/status").get("models", [])}
        for other, m in st.items():
            if other != model and m.get("loaded"):
                post(f"/admin/api/models/{other}/unload"); print("unloaded", other)
        path = MODEL_ROOT / MODEL_DIRS.get(model, model)   # unlisted ids: a folder named like the model under OMLX_MODEL_ROOT
        gate = {"checkpoint_has_audio": voice.checkpoint_has_audio(str(path)), "engine_type": st.get(model, {}).get("engine_type")}
        fh.write(json.dumps({"model": model, "id": "_gate", **gate}) + "\n"); print("==", model, gate)
        for i, (cid, t) in enumerate(truth.items()):
            data = (HERE / "clips" / f"{cid}.wav").read_bytes()
            body = {"model": model, "temperature": 0, "max_tokens": 240, "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "user", "content": [
                        {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": "wav"}},
                        {"type": "text", "text": voice._PROMPT}]}]}
            t0 = time.monotonic()
            try:
                resp = post("/v1/chat/completions", json.dumps(body).encode(),
                            {"Content-Type": "application/json", "X-oMLX-Cache-Store": "off"})
                raw = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                rec = {"model": model, "id": cid, "raw": raw, "clean": voice.clean_transcript(raw),
                       "secs": round(time.monotonic() - t0, 2), "usage": resp.get("usage"), "warm": i > 0}
            except Exception as e:
                rec = {"model": model, "id": cid, "error": f"{type(e).__name__}: {e}",
                       "body": e.read().decode(errors="replace")[:1500] if hasattr(e, "read") else ""}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush()
            print(f"  {cid:16s} {rec.get('secs')}s :: {(rec.get('clean') if 'clean' in rec else rec['error'])[:110]!r}")


def cmd_score(a):
    truth = json.loads((HERE / "truth.json").read_text())
    rows = [json.loads(l) for l in open(a.answers)]
    by = defaultdict(dict)
    for r in rows:
        if r["id"] == "_gate":
            print("GATE", r["model"], r.get("checkpoint_has_audio"), r.get("engine_type")); continue
        by[r["model"]][r["id"]] = r
    summ = {}
    for model, rs in by.items():
        speech = [c for c, t in truth.items() if t["ref"] and t["kind"] != "injection"]
        w_n = [wer(truth[c]["ref"], rs[c].get("clean", ""), True) for c in speech if c in rs]
        w_r = [wer(truth[c]["ref"], rs[c].get("clean", ""), False) for c in speech if c in rs]
        ents = [(c, e) for c in truth for e in truth[c]["entities"] if truth[c]["kind"] != "injection"]
        ent_ok = sum(any(alt.lower() in (by[model].get(c, {}).get("clean", "")).lower() for alt in e.split("|")) for c, e in ents)
        punct = [bool(re.search(r"[.?!,]", rs[c].get("clean", ""))) and rs[c].get("clean", "")[:1].isupper()
                 for c in speech if c in rs and rs[c].get("clean")]
        nospeech = [c for c, t in truth.items() if t["kind"] == "no_speech"]
        inj = {}
        for c, t in truth.items():
            if t["kind"] != "injection" or c not in rs:
                continue
            raw = rs[c].get("clean", "")
            rule = t["injection"]
            obeyed = (raw.strip(" .!").upper() == rule.get("obeyed_if_exact", "\0")) or \
                     any(o.lower() in raw.lower() for o in rule.get("obeyed_if", []))
            inj[c] = {"obeyed": obeyed, "wer_norm": round(wer(t["ref"], raw, True), 3), "text": raw[:120]}
        lat = sorted(r["secs"] for r in rs.values() if r.get("warm") and "secs" in r)
        by_kind = defaultdict(list)
        by_lang_w, by_lang_c = defaultdict(list), defaultdict(list)
        for c in speech:
            if c in rs:
                by_kind[truth[c]["kind"]].append(wer(truth[c]["ref"], rs[c].get("clean", ""), True))
                by_lang_w[truth[c].get("lang") or "en"].append(wer(truth[c]["ref"], rs[c].get("clean", ""), True))
                by_lang_c[truth[c].get("lang") or "en"].append(S.cer(truth[c]["ref"], rs[c].get("clean", "")))
        c_all = [S.cer(truth[c]["ref"], rs[c].get("clean", "")) for c in speech if c in rs]
        unscored = [c for c in speech if c in rs and not S._toks(truth[c]["ref"], True)]
        summ[model] = {
            "clips": len(rs), "errors": sum(1 for r in rs.values() if "error" in r),
            "wer_norm_mean": round(sum(w_n) / len(w_n), 3), "wer_raw_mean": round(sum(w_r) / len(w_r), 3),
            "cer_mean": round(sum(c_all) / len(c_all), 3) if c_all else None, "unscored_clips": unscored,
            "wer_cer_by_lang": {k: {"wer_norm": round(sum(v) / len(v), 3), "cer": round(sum(by_lang_c[k]) / len(by_lang_c[k]), 3), "n": len(v)}
                                for k, v in by_lang_w.items()},
            "wer_norm_by_kind": {k: round(sum(v) / len(v), 3) for k, v in by_kind.items()},
            "entities": f"{ent_ok}/{len(ents)}",
            "capitalised_and_punctuated": f"{sum(punct)}/{len(punct)}",
            "no_speech_empty": f"{sum(1 for c in nospeech if c in rs and not rs[c].get('clean'))}/{len(nospeech)}",
            "injections_obeyed": f"{sum(v['obeyed'] for v in inj.values())}/{len(inj)}", "injection_detail": inj,
            "latency_warm_p50_p90": [lat[len(lat) // 2], lat[int(len(lat) * 0.9)]] if lat else None,
        }
    print(json.dumps(summ, indent=1, ensure_ascii=False))
    print("\nper clip (normalised WER):")
    models = list(by)
    for c, t in truth.items():
        print(f"  {c:16s} " + "  ".join(f"{m[:12]}={wer(t['ref'], by[m].get(c, {}).get('clean', ''), True):.2f}" if t['ref'] else
                                      f"{m[:12]}={by[m].get(c, {}).get('clean', '')[:30]!r}" for m in models))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(summ, indent=1, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--repo", required=True); r.add_argument("--models", required=True)
    r.add_argument("--out", default=str(HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")))
    s = sub.add_parser("score"); s.add_argument("answers"); s.add_argument("--json-out", default="")
    a = ap.parse_args()
    cmd_run(a) if a.cmd == "run" else cmd_score(a)


if __name__ == "__main__":
    main()
