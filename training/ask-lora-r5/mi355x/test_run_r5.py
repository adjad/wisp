"""run_r5.sh on a stubbed box: one A6000 runs the picker one model at a time on GPU 0, eight GPUs run it side by side, and
training commands reach run_sft.sh on NVIDIA as well as AMD (the user moved r5's training to the A6000 on 2026-11-08)."""
import json
import os
import stat
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
RUN = HERE / "run_r5.sh"

# The stub python: inline heredocs (`$PY -`) run on the real interpreter; every other call logs its GPU and arguments and
# writes what the next step reads.
PY_STUB = r"""#!/bin/bash
if [ "$1" = - ]; then exec python3 "$@"; fi
echo "gpu=${CUDA_VISIBLE_DEVICES:-unset} $(basename "$1") $2 $*" >> "$LOG"
sleep 0.2
case "$(basename "$1") $2" in
  "pick_text.py candidates") echo "ckpt-100,ckpt-200" ;;
  "pick_text.py generate")
    out=""; prev=""; for a in "$@"; do [ "$prev" = --out ] && out="$a"; prev="$a"; done
    mkdir -p "$out"; echo '{"ckpt": "x"}' > "$out/text_answers.jsonl"; echo '{}' > "$out/text_run_info.json" ;;
  "pick_text.py pick") echo "/top/ckpt-200" ;;
esac
exit 0
"""


def _exe(p: Path, text: str) -> None:
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


def _box(tmp: Path, gpus: int) -> dict:
    wisp = tmp / "wisp"
    (wisp / "venv" / "bin").mkdir(parents=True)
    _exe(wisp / "venv" / "bin" / "python", PY_STUB)
    mm = wisp / "mmeval"
    (mm / "data").mkdir(parents=True)
    for f in ("pick_text.py", "eval_mm.py"):
        (mm / f).write_text("")
    (mm / "data" / "text-dev.jsonl").write_text("{}\n")
    (wisp / "gemma" / "ask-lora-out" / "ckpt").mkdir(parents=True)
    (wisp / "gemma" / "base_path").write_text("/base\n")
    bin_ = tmp / "bin"
    bin_.mkdir()
    _exe(bin_ / "nvidia-smi", "#!/bin/bash\n" + "".join(f"echo 'GPU {i}: NVIDIA RTX A6000 (UUID: GPU-{i})'\n" for i in range(gpus)))
    log = tmp / "calls.log"
    return {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "WISP": str(wisp), "LOG": str(log), "HOME": str(tmp)}


def _calls(env) -> list[str]:
    return Path(env["LOG"]).read_text().splitlines()


def test_one_a6000_runs_the_picker_one_model_at_a_time_on_gpu_0(tmp_path):
    env = _box(tmp_path, 1)
    r = subprocess.run(["bash", str(RUN), "pick"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "one after another" in r.stdout and "/top/ckpt-200" in r.stdout
    calls = _calls(env)
    runs = [c for c in calls if " eval_mm.py " in c or " generate " in c]
    assert len(runs) == 4 and all(c.startswith("gpu=0 ") for c in runs)           # mmeval, base, two candidates
    assert all('--batch 8' in c for c in runs if " generate " in c)
    assert json.loads((tmp_path / "wisp/gemma/pick/text/text_run_info.json").read_text())["checkpoints"] == ["base", "ckpt-100", "ckpt-200"]


def test_a_failed_model_on_one_gpu_stops_the_pick(tmp_path):
    env = _box(tmp_path, 1)
    py = Path(env["WISP"]) / "venv/bin/python"
    _exe(py, PY_STUB.replace('esac\nexit 0', 'esac\ncase "$*" in *"--ckpts ckpt-100 --no-base"*) exit 3;; esac\nexit 0'))
    r = subprocess.run(["bash", str(RUN), "pick"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "text-ckpt-100 failed" in r.stderr
    assert not any("ckpt-200" in c and " generate " in c for c in _calls(env))


def test_eight_gpus_still_run_the_picker_side_by_side(tmp_path):
    env = _box(tmp_path, 8)
    r = subprocess.run(["bash", str(RUN), "pick"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "side by side" in r.stdout
    gpus = {c.split()[0] for c in _calls(env) if " generate " in c}
    assert gpus == {"gpu=0", "gpu=1", "gpu=2"}
    assert any(c.startswith("gpu=7 ") and " eval_mm.py " in c for c in _calls(env))


def test_training_commands_reach_run_sft_on_the_a6000(tmp_path):
    env = _box(tmp_path, 1)
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "run_r5.sh").write_text(RUN.read_text())
    _exe(kit / "run_sft.sh", '#!/bin/bash\necho "run_sft $* ATTN=$ATTN NPROC=$NPROC"\n')
    for cmd in ("unpack", "preflight", "smoke", "train", "status", "merge", "package", "bundle"):
        r = subprocess.run(["bash", str(kit / "run_r5.sh"), cmd], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip() == f"run_sft {cmd} ATTN=sdpa NPROC=1", (cmd, r.stdout, r.stderr)
