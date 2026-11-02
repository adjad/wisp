#!/usr/bin/env python3
"""Video check: vision.answer_request's clip path, replayed with direct oMLX calls: the audio track through
voice.py's transcription request, then vision._messages(prompt, frames, transcript, seconds) at temperature 0,
thinking off. Two synthetic clips (frames drawn with PIL, narration from macOS `say`).

    run_video.py make
    run_video.py run --repo <main checkout> --models A,B --out DIR
"""
import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VID = HERE / "video"
BASE = os.environ.get("OMLX_BASE", "http://127.0.0.1:8000")


def make():
    from PIL import Image, ImageDraw, ImageFont
    f = lambda s: ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", s)
    VID.mkdir(exist_ok=True)
    clips = {
        "vid_whiteboard": {"seconds": 8, "narration": "Okay so we agreed, design review moves to Thursday at three, room twelve.",
                           "prompt": "What did we agree on in this clip?", "must": ["Thursday", "3|three", "12|twelve"],
                           "frames": [["Design review"], ["Design review", "moves to THU"], ["Design review", "moves to THU", "3 PM"],
                                      ["Design review", "moves to THU", "3 PM", "Room 12"]]},
        "vid_oven": {"seconds": 6, "narration": "The cake still needs twenty five more minutes, then let it cool.",
                     "prompt": "How much longer does the cake need?", "must": ["25|twenty five|twenty-five"],
                     "frames": [["OVEN 350°F"], ["OVEN 350°F", "TIMER 25:00"], ["OVEN 350°F", "TIMER 24:58"]]},
    }
    for cid, c in clips.items():
        frames = []
        for rows in c["frames"]:
            im = Image.new("RGB", (960, 540), "#f7f7f2"); d = ImageDraw.Draw(im)
            for i, t in enumerate(rows):
                d.text((60, 60 + i * 90), t, font=f(56), fill="#1d3557")
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
            frames.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
        wav = VID / f"{cid}.wav"
        subprocess.run(["say", "-v", "Samantha", "-o", str(wav), "--data-format=LEI16@16000", c["narration"]], check=True)
        c["frame_urls"] = frames
        c["audio_b64"] = base64.b64encode(wav.read_bytes()).decode()
    (VID / "clips.json").write_text(json.dumps(clips))
    print("made", list(clips))


def post(path, body, headers=None):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-oMLX-Cache-Store": "off", **(headers or {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def run(a):
    os.environ.setdefault("MOE_DIR", tempfile.mkdtemp(prefix="videoeval-moe-"))
    os.environ.setdefault("WISP_HOME", os.environ["MOE_DIR"])
    sys.path.insert(0, a.repo)
    from service import vision, voice
    clips = json.loads((VID / "clips.json").read_text())
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    fh = open(out / "video_answers.jsonl", "a")
    for model in a.models.split(","):
        for cid, c in clips.items():
            t0 = time.monotonic()
            tr = post("/v1/chat/completions", {"model": model, "temperature": 0, "max_tokens": 240,
                                               "chat_template_kwargs": {"enable_thinking": False},
                                               "messages": [{"role": "user", "content": [
                                                   {"type": "input_audio", "input_audio": {"data": c["audio_b64"], "format": "wav"}},
                                                   {"type": "text", "text": voice._PROMPT}]}]})
            transcript = voice.clean_transcript(((tr.get("choices") or [{}])[0].get("message") or {}).get("content") or "") or None
            msgs = vision._messages(c["prompt"], c["frame_urls"], transcript, float(c["seconds"]))
            resp = post("/v1/chat/completions", {"model": model, "messages": msgs, "temperature": 0, "max_tokens": 400,
                                                 "chat_template_kwargs": {"enable_thinking": False}})
            raw = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            visible, proposal = vision.parse_proposal(vision._THINK.sub("", raw))
            hit = [any(x.lower() in raw.lower() for x in m.split("|")) for m in c["must"]]
            rec = {"model": model, "id": cid, "transcript": transcript, "answer": raw, "proposal": proposal,
                   "facts": f"{sum(hit)}/{len(hit)}", "secs": round(time.monotonic() - t0, 2)}
            fh.write(json.dumps(rec) + "\n"); fh.flush()
            print(model[:30], cid, rec["facts"], rec["secs"], "|", (transcript or "")[:60], "|", raw[:120].replace("\n", " "))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make")
    r = sub.add_parser("run"); r.add_argument("--repo", required=True); r.add_argument("--models", required=True); r.add_argument("--out", required=True)
    a = ap.parse_args()
    make() if a.cmd == "make" else run(a)
