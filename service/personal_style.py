"""Your writing style: a small adapter trained on this Mac from your own sent mail.

When the person asks for it, Wisp reads the emails they sent (Apple Mail, on this
Mac), trains a LoRA adapter on the chat model so reply drafts sound like them, and
uses that adapter for one job only: the reply draft the person reads, edits and
sends themselves from the Brief (assistant/email_reply_api._model_reply).

IMPORTANT rules:
- Only when asked. Training starts from the person's own press of the button,
  never on a schedule. It can be stopped, and forgotten.
- Nothing leaves the Mac. Training and drafting run in a child process on this
  Mac (personal_style_worker.py and `python -m mlx_lm lora`); neither has any
  reason to open a network connection, and the model is the one already on disk.
- Drafts only, never tools. The adapter is not loaded by the engine and never
  sees a tool schema. It writes text into an editable field; sending is still
  the person's own press, through the same reviewed path as before. Every tuned
  Ask model so far took more unsafe actions than the base (r2 to r5), which is
  why this one is kept away from anything that can act.
- Only the person's own words are learned. The email being answered is in the
  prompt as fenced data and is masked out of the loss (`--mask-prompt`); the
  loss is on the reply the person wrote.
- The training text is deleted when training ends, whatever the outcome. What
  stays is the adapter (about 14 MB) and a small state file, private, under
  ~/.moe/personal_style/. An adapter can reproduce phrases from the mail it was
  trained on, so it is erased by Forget, by "Forget what Wisp learned from
  mail", and by /resetwisp.
- Any failure falls back to the usual model. A draft is never blocked by this.
- The adapter belongs to the model it was trained on. If the chat model
  changes, the adapter is not used until it is trained again.
- Wisp's service does not import MLX. This needs a Python that has `mlx_lm`:
  the one inside the person's oMLX.app, or `WISP_MLX_PYTHON`. Without one the
  feature says so and stays off.
- Training yields to the person: the child is paused (SIGSTOP) while a
  foreground turn is running and resumed after it.

MEASURED 2026-11-11 on an M5 Pro 24 GB, gemma-4-E4B-it-oQ4e-mtp, oMLX.app's
Python (mlx_lm 0.32.3): 10 steps at batch 1, 8 layers, 512 tokens took 12 s
including the load, peak footprint 5.8 GB beside the resident chat model; one
draft through the worker took 5.9 s including the load.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from service import paths

MIN_EXAMPLES = 12
MAX_EXAMPLES = 300
WINDOW_DAYS = 200
_BODY_MIN, _BODY_MAX = 20, 1500
_LAYERS = 8
_MAX_SEQ = 768
_WRITE_TIMEOUT_S = 60.0
_TRAIN_TIMEOUT_S = 3 * 3600.0
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ITER = re.compile(r"^\s*(\d+)\s+(val\s+)?(\d+\.\d+)")

_OMLX_PYTHON_ROOT = Path("/Applications/oMLX.app/Contents/Resources/Python")

_task: asyncio.Task | None = None
_child: asyncio.subprocess.Process | None = None
_live: dict[str, Any] = {}


class StyleError(Exception):
    """A request that cannot be done; the message is for the person."""


def _dir() -> Path:
    return paths.moe_dir() / "personal_style"


def _state() -> dict[str, Any]:
    try:
        state = json.loads((_dir() / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    paths.private_mkdirs(_dir())
    paths.private_write_bytes(_dir() / "state.json", json.dumps(state).encode("utf-8"))


def mlx_python() -> tuple[str, dict[str, str]] | None:
    """(interpreter, extra environment) for a Python that has mlx_lm, or None."""
    override = os.environ.get("WISP_MLX_PYTHON")
    if override:
        return (override, {}) if Path(override).is_file() else None
    exes = sorted(_OMLX_PYTHON_ROOT.glob("cpython-*/bin/python3"))
    sites = sorted(_OMLX_PYTHON_ROOT.glob("framework-mlx-base/lib/python3*/site-packages"))
    if exes and sites and (sites[-1] / "mlx_lm").is_dir():
        return str(exes[-1]), {"PYTHONPATH": str(sites[-1])}
    return None


def _adapter() -> Path:
    return _dir() / "adapter"


def _has_adapter() -> bool:
    return (_adapter() / "adapters.safetensors").is_file() and (_adapter() / "adapter_config.json").is_file()


def _chat_model() -> str:
    from service.config import role_to_model
    return role_to_model("fast")


def status() -> dict[str, Any]:
    """What the Settings row shows. Counts and fixed words only; no mail text."""
    state = _state()
    training = _task is not None and not _task.done()
    trained = _has_adapter()
    try:
        current = trained and state.get("model") == _chat_model()
    except Exception:  # noqa: BLE001 — an unreadable roster is not a reason to hide the row
        current = False
    return {"available": mlx_python() is not None,
            "enabled": bool(state.get("enabled")),
            "phase": _live.get("phase", "idle") if training else "idle",
            "step": _live.get("step", 0) if training else 0,
            "steps": _live.get("steps", 0) if training else 0,
            "trained": trained,
            "current": bool(current),
            "examples": int(state.get("examples") or 0),
            "trained_at": state.get("trained_at"),
            "loss": state.get("loss"),
            "error": state.get("error") or ""}


def set_enabled(enabled: bool) -> dict[str, Any]:
    state = _state()
    state["enabled"] = bool(enabled)
    _save(state)
    return status()


def ready() -> bool:
    """Drafts may use the adapter: switched on, trained on the current chat model, not mid-training."""
    if _task is not None and not _task.done():
        return False
    state = _state()
    try:
        return bool(state.get("enabled")) and _has_adapter() and state.get("model") == _chat_model()
    except Exception:  # noqa: BLE001
        return False


# --- examples ----------------------------------------------------------------------------

def _usable(body: str) -> bool:
    return _BODY_MIN <= len(body.strip()) <= _BODY_MAX


def example(parent: dict | None, sent: dict) -> dict[str, Any]:
    """One training row: the same prompt a reply draft gets, answered by what the person wrote."""
    from service.assistant.email_reply_api import _fence, draft_prompt
    if parent:
        system, material = draft_prompt(parent)
    else:
        system, _ = draft_prompt({"body": "-"})
        material = ("Write the user's email.\n\n" + _fence(f"To: {sent.get('to') or ''}\n"
                                                            f"Subject: {sent.get('subject') or ''}"))
    return {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": material},
                         {"role": "assistant", "content": sent["body"].strip()}]}


async def collect(progress=None) -> list[dict[str, Any]]:
    """Training rows from the person's sent mail in Apple Mail, newest first.

    Raises StyleError when the app is not connected or Mail cannot be read.
    """
    from service.mail_memory import bridge
    from service.mail_memory.extract import parse_raw
    try:
        rows, _capped = await bridge.read_index(WINDOW_DAYS)
    except bridge.BridgeError:
        raise StyleError("Wisp couldn't read Mail. Open Wisp, check Full Disk Access, and try again.") from None
    sent = sorted((r for r in rows if r.get("box") == "sent"), key=lambda r: r.get("ts") or 0, reverse=True)
    inbox: dict[Any, list[dict]] = {}
    for row in rows:
        if row.get("box") == "inbox" and row.get("conversation_id"):
            inbox.setdefault(row["conversation_id"], []).append(row)

    async def read(row: dict) -> dict | None:
        try:
            raw, _why = await bridge.read_body(int(row["rowid"]))
            if raw is None:
                return None
            mail = parse_raw(raw, float(row.get("ts") or 0))
        except (bridge.BridgeError, ValueError, KeyError, TypeError):
            return None
        return {"sender": mail.sender, "to": mail.to, "subject": mail.subject,
                "ts": row.get("ts"), "body": mail.body}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sent[:MAX_EXAMPLES * 2]:
        if len(out) >= MAX_EXAMPLES:
            break
        mine = await read(row)
        if not mine or not _usable(mine["body"]) or mine["body"] in seen:
            continue
        seen.add(mine["body"])
        earlier = [r for r in inbox.get(row.get("conversation_id"), [])
                   if (r.get("ts") or 0) < (row.get("ts") or 0)]
        parent = await read(max(earlier, key=lambda r: r.get("ts") or 0)) if earlier else None
        out.append(example(parent, mine))
        if progress:
            progress(len(out))
    return out


# --- training ----------------------------------------------------------------------------

def steps_for(examples: int) -> int:
    """About four passes over the examples, between 60 and 400 steps."""
    return max(60, min(400, examples * 4))


async def _stage_base(model: str) -> Path:
    """A folder of links to the chat model's files, without the MTP head mlx_lm cannot load."""
    from service.tools.imessage_tools import _c
    status_reply = await _c().status()
    row = next((m for m in status_reply.get("models") or [] if m.get("id") == model), None)
    source = Path(str((row or {}).get("model_path") or ""))
    if not (source / "config.json").is_file():
        raise StyleError("Wisp couldn't find the chat model's files on this Mac.")
    base = _dir() / "base"
    shutil.rmtree(base, ignore_errors=True)
    paths.private_mkdirs(base)
    for item in source.iterdir():
        if item.name != "model-mtp.safetensors" and not item.name.startswith("."):
            os.symlink(item, base / item.name)
    return base


def _write_rows(folder: Path, rows: list[dict]) -> None:
    paths.private_mkdirs(folder)
    held_out = max(1, len(rows) // 10)
    for name, part in (("valid", rows[:held_out]), ("train", rows[held_out:])):
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in part) + "\n"
        paths.private_write_bytes(folder / f"{name}.jsonl", text.encode("utf-8"))


async def _pause_for_turns(child: asyncio.subprocess.Process) -> None:
    """Stop the trainer while the person is being answered; let it run again after."""
    from service import idle
    paused = False
    try:
        while child.returncode is None:
            busy = idle.foreground_busy()
            if busy != paused:
                try:
                    child.send_signal(signal.SIGSTOP if busy else signal.SIGCONT)
                except ProcessLookupError:
                    return
                paused = busy
            await asyncio.sleep(0.5)
    finally:
        if paused and child.returncode is None:
            try:
                child.send_signal(signal.SIGCONT)
            except ProcessLookupError:
                pass


async def _run_trainer(base: Path, data: Path, out: Path, steps: int) -> float | None:
    """Run `mlx_lm lora`; the last validation loss, or raise StyleError."""
    global _child
    python = mlx_python()
    if python is None:
        raise StyleError("This needs oMLX's Python on this Mac, and Wisp couldn't find it.")
    exe, extra = python
    command = [exe, "-m", "mlx_lm", "lora", "--model", str(base), "--train", "--data", str(data),
               "--adapter-path", str(out), "--iters", str(steps), "--batch-size", "1",
               "--num-layers", str(_LAYERS), "--max-seq-length", str(_MAX_SEQ), "--mask-prompt",
               "--steps-per-report", "5", "--steps-per-eval", str(max(20, steps // 4)),
               "--val-batches", "8", "--save-every", str(steps)]
    env = {**os.environ, **extra, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    child = await asyncio.create_subprocess_exec(
        *command, cwd=str(_dir()), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    _child = child
    watcher = asyncio.create_task(_pause_for_turns(child))
    loss: float | None = None
    try:
        async def read() -> None:
            nonlocal loss
            assert child.stdout is not None
            async for raw in child.stdout:
                match = _ITER.match(_ANSI.sub("", raw.decode("utf-8", "replace")))
                if match:
                    _live["step"] = min(steps, int(match.group(1)))
                    if match.group(2):
                        loss = float(match.group(3))
        await asyncio.wait_for(asyncio.gather(read(), child.wait()), timeout=_TRAIN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise StyleError("Training took too long and was stopped.") from None
    finally:
        watcher.cancel()
        if child.returncode is None:
            try:
                child.send_signal(signal.SIGCONT)
                child.kill()
            except ProcessLookupError:
                pass
            await child.wait()
        _child = None
    if child.returncode != 0 or not (out / "adapters.safetensors").is_file():
        raise StyleError("Training didn't finish. Nothing was changed.")
    return loss


async def _train(rows: list[dict] | None) -> None:
    state = _state()
    state["error"] = ""
    _save(state)
    data, fresh = _dir() / "data", _dir() / "adapter.new"
    try:
        _live.update(phase="reading", step=0, steps=0)
        model = _chat_model()
        if rows is None:
            rows = await collect(lambda n: _live.update(step=n))
        if len(rows) < MIN_EXAMPLES:
            raise StyleError(f"Wisp found {len(rows)} sent emails it can learn from, "
                             f"and needs at least {MIN_EXAMPLES}.")
        base = await _stage_base(model)
        _write_rows(data, rows)
        shutil.rmtree(fresh, ignore_errors=True)
        steps = steps_for(len(rows))
        _live.update(phase="training", step=0, steps=steps)
        loss = await _run_trainer(base, data, fresh, steps)
        for extra in fresh.glob("0*_adapters.safetensors"):
            extra.unlink()
        shutil.rmtree(_adapter(), ignore_errors=True)
        os.replace(fresh, _adapter())
        state = _state()
        state.update(model=model, examples=len(rows), trained_at=time.time(), loss=loss, error="")
        state.setdefault("enabled", True)
        _save(state)
    except StyleError as error:
        state = _state()
        state["error"] = str(error)
        _save(state)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — say it failed; the old adapter, if any, is untouched
        state = _state()
        state["error"] = "Training didn't finish. Nothing was changed."
        _save(state)
    finally:
        # The training text is the person's mail: it never outlives the run.
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(fresh, ignore_errors=True)
        _live.clear()


def start_training(rows: list[dict] | None = None) -> dict[str, Any]:
    """Begin learning from sent mail. `rows` is for tests; the app never supplies them."""
    global _task
    if _task is not None and not _task.done():
        raise StyleError("Wisp is already learning your writing style.")
    if mlx_python() is None:
        raise StyleError("This needs oMLX's Python on this Mac, and Wisp couldn't find it.")
    _live.update(phase="reading", step=0, steps=0)
    _task = asyncio.get_running_loop().create_task(_train(rows))
    return status()


async def stop() -> dict[str, Any]:
    """Stop a run in progress. The adapter from an earlier run, if any, stays."""
    task = _task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(task, return_exceptions=True)), timeout=20)
        except asyncio.TimeoutError:
            pass
    return status()


async def forget() -> bool:
    """Stop any run and erase the adapter, the staged links and the state. True when nothing is left."""
    await stop()
    shutil.rmtree(_dir(), ignore_errors=True)
    return not _dir().exists()


# --- drafting ----------------------------------------------------------------------------

async def write(system: str, material: str, *, max_tokens: int = 320) -> str | None:
    """A reply draft in the person's style, or None to use the usual model.

    The caller already holds the foreground gate. One short-lived child loads the
    chat model's files with the adapter, writes, and exits.
    """
    if not ready():
        return None
    python = mlx_python()
    base = _dir() / "base"
    if python is None or not (base / "config.json").exists():
        return None
    exe, extra = python
    request = json.dumps({"model": str(base), "adapter": str(_adapter()), "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": material}]}).encode("utf-8")
    worker = Path(__file__).with_name("personal_style_worker.py")
    env = {**os.environ, **extra, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    child = None
    try:
        child = await asyncio.create_subprocess_exec(
            exe, str(worker), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(child.communicate(request), timeout=_WRITE_TIMEOUT_S)
        text = json.loads(out.decode("utf-8")).get("text")
    except Exception:  # noqa: BLE001 — any failure means the usual model writes the draft
        if child is not None and child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    state = _state()
    state["drafts"] = int(state.get("drafts") or 0) + 1
    _save(state)
    return text.strip()
