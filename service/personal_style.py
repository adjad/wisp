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
  the one inside the person's oMLX.app (`WISP_MLX_PYTHON` is honoured only by
  source runs and preview builds). Without one the feature says so and stays off.
- That Python is run locked down: `-I -S` (no environment, no user site, no
  `.pth` or sitecustomize startup code), the one site-packages folder named
  explicitly, a minimal environment, and never with `service/` on its path.
  A tree with a foreign owner, a world-writable entry, or an interpreter that
  leaves the tree is refused.
- Memory. Training takes about 6 GB beside the chat model and every draft loads
  a second copy of the model, so both need a Mac with 24 GB or more
  (MIN_MEMORY_GB). On a smaller Mac the row says so and drafts use the usual
  model. A paused trainer still holds its memory.
- Training yields to the person: the child is paused (SIGSTOP) while a
  foreground turn is running and resumed after it.
- A crash or kill leaves nothing behind for long: the trainer's pid is recorded,
  and the next start kills an orphan and deletes the training text
  (`recover`). Shutdown stops a run.

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
import stat
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
MIN_MEMORY_GB = 24
_WRITE_TIMEOUT_S = 25.0
_TREE_TTL_S = 300.0
_TREE_MAX_ENTRIES = 200_000
_TRAIN_TIMEOUT_S = 3 * 3600.0
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ITER = re.compile(r"^\s*(\d+)\s+(val\s+)?(\d+\.\d+)")

_OMLX_PYTHON_ROOT = Path("/Applications/oMLX.app/Contents/Resources/Python")

# Run a module, or a script, with exactly one extra import folder and no startup code.
_BOOT = ("import runpy,sys\n"
         "site=sys.argv[1]\n"
         "sys.argv=['mlx_lm']+sys.argv[2:]\n"
         "sys.path.append(site)\n"
         "runpy.run_module('mlx_lm',run_name='__main__',alter_sys=True)\n")

_task: asyncio.Task | None = None
_child: asyncio.subprocess.Process | None = None
_live: dict[str, Any] = {}
_tree_checked: dict[str, tuple[float, int, bool]] = {}


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


def memory_gb() -> int:
    """This Mac's memory in whole GB; 0 when it cannot be read (treated as too little)."""
    try:
        return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30)
    except (ValueError, OSError, AttributeError):
        return 0


def enough_memory() -> bool:
    return memory_gb() >= MIN_MEMORY_GB


_MEMORY_WORDS = f"This needs a Mac with {MIN_MEMORY_GB} GB of memory or more."


def _override_allowed() -> bool:
    """`WISP_MLX_PYTHON` counts in a source run and in a preview build, never in a release."""
    plist = Path(__file__).resolve().parents[3] / "Info.plist"
    if not plist.is_file():
        return True
    try:
        import plistlib
        with plist.open("rb") as handle:
            return plistlib.load(handle).get("WispPreviewBuild") is True
    except Exception:  # noqa: BLE001 — an unreadable plist is a release as far as this goes
        return False


def _tree_qualifies(root: Path) -> bool:
    """No entry of the tree has a foreign owner or is writable by everyone.

    local_peer.runtime_manifest's ownership and write rules, without its
    hashing or its layout pin (this is the person's own oMLX.app, not a
    runtime Wisp materialized). Start-up code is dealt with by not running it
    (`-I -S`), so a `.pth` or sitecustomize in the tree cannot execute.
    """
    try:
        key, stamp = str(root), root.lstat().st_mtime_ns
    except OSError:
        return False
    seen = _tree_checked.get(key)
    if seen and seen[1] == stamp and time.monotonic() - seen[0] < _TREE_TTL_S:
        return seen[2]
    ok, count, owners = True, 0, (0, os.getuid())
    try:
        for folder, names, files in os.walk(root, followlinks=False):
            for name in ("", *names, *files):
                info = os.lstat(os.path.join(folder, name) if name else folder)
                count += 1
                if (count > _TREE_MAX_ENTRIES or info.st_uid not in owners
                        or (info.st_mode & 0o002 and not stat.S_ISLNK(info.st_mode))):
                    ok = False
                    break
            if not ok:
                break
    except OSError:
        ok = False
    _tree_checked[key] = (time.monotonic(), stamp, ok)
    return ok


def mlx_python() -> tuple[str, str] | None:
    """(interpreter, its mlx site-packages or "") for a Python that has mlx_lm, or None."""
    override = os.environ.get("WISP_MLX_PYTHON")
    if override and _override_allowed():
        return (override, "") if Path(override).is_file() else None
    exes = sorted(_OMLX_PYTHON_ROOT.glob("cpython-*/bin/python3"))
    sites = sorted(_OMLX_PYTHON_ROOT.glob("framework-mlx-base/lib/python3*/site-packages"))
    if not (exes and sites and (sites[-1] / "mlx_lm").is_dir()):
        return None
    try:
        root = _OMLX_PYTHON_ROOT.resolve(strict=True)
        exe, site = exes[-1].resolve(strict=True), sites[-1].resolve(strict=True)
    except OSError:
        return None
    # The interpreter and the packages are inside the tree that was checked, not linked out of it.
    if root not in exe.parents or root not in site.parents or not _tree_qualifies(root):
        return None
    return str(exe), str(site)


def _command(python: tuple[str, str], *, module_args: list[str] | None = None,
             script: Path | None = None) -> list[str]:
    """The child's argv. `-I`: no PYTHON* variables, no user site, no script folder or cwd
    on the path. `-S` (oMLX's tree): no site start-up, so no `.pth` and no sitecustomize."""
    exe, site = python
    flags = ["-I", "-S"] if site else ["-I"]
    if script is not None:
        return [exe, *flags, str(script), site]
    return [exe, *flags, "-c", _BOOT, site, *(module_args or [])]


def _child_env() -> dict[str, str]:
    """What a child needs and nothing else: no tokens, no proxies, no PYTHON* (ignored anyway)."""
    env = {"PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HOME": os.environ.get("HOME", "")}
    for name in ("TMPDIR", "LANG", "LC_ALL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if os.environ.get("WISP_MLX_PYTHON") and _override_allowed():   # the tests' stand-in reads these
        env.update({k: v for k, v in os.environ.items() if k.startswith("STYLE_STUB_")})
    return env


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
            "enough_memory": enough_memory(),
            "memory_needed_gb": MIN_MEMORY_GB,
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
    if (_task is not None and not _task.done()) or not enough_memory():
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
    command = _command(python, module_args=[
        "lora", "--model", str(base), "--train", "--data", str(data),
        "--adapter-path", str(out), "--iters", str(steps), "--batch-size", "1",
        "--num-layers", str(_LAYERS), "--max-seq-length", str(_MAX_SEQ), "--mask-prompt",
        "--steps-per-report", "5", "--steps-per-eval", str(max(20, steps // 4)),
        "--val-batches", "8", "--save-every", str(steps)])
    child = await asyncio.create_subprocess_exec(
        *command, cwd=str(_dir()), env=_child_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    _child = child
    _record_child(child.pid)
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
        _forget_child()
    if child.returncode != 0 or not (out / "adapters.safetensors").is_file():
        raise StyleError("Training didn't finish. Nothing was changed.")
    return loss


# --- a run that was cut off ----------------------------------------------------------------

def _pid_file() -> Path:
    return _dir() / "trainer.json"


def _started(pid: int) -> list[int] | None:
    """When `pid` started, from the kernel (libproc); None when it is gone or cannot be read.
    A pid alone can be reused by another program; a pid with its start time cannot."""
    try:
        from service.inference.local_peer import process_bsdinfo
        info = process_bsdinfo(pid)
        return [info[7], info[8]] if info[2] == os.getuid() else None
    except Exception:  # noqa: BLE001
        return None


def _record_child(pid: int) -> None:
    try:
        paths.private_write_bytes(_pid_file(), json.dumps({"pid": pid, "started": _started(pid)}).encode("utf-8"))
    except OSError:
        pass


def _forget_child() -> None:
    try:
        _pid_file().unlink()
    except OSError:
        pass


def recover() -> bool:
    """Called at startup. Kill a trainer orphaned by a crash or kill (it may be paused
    forever) and delete the training text it left. True when something was cleaned up."""
    if _task is not None and not _task.done():
        return False
    cleaned = False
    try:
        record = json.loads(_pid_file().read_text(encoding="utf-8"))
        pid, started = record["pid"], record["started"]
    except (OSError, ValueError, KeyError, TypeError):
        pid, started = 0, None
    # Still the same process (same pid AND same start time), so it is the trainer this file
    # recorded and nothing else. It may be paused: continue it so the kill is delivered.
    if type(pid) is int and pid > 1 and started and _started(pid) == started:
        for sig in (signal.SIGCONT, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
        cleaned = True
    _forget_child()
    for left in (_dir() / "data", _dir() / "adapter.new"):
        if left.exists():
            shutil.rmtree(left, ignore_errors=True)
            cleaned = True
    return cleaned


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
    if not enough_memory():
        raise StyleError(_MEMORY_WORDS)
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

async def write(system: str, material: str, *, max_tokens: int = 320,
                timeout: float = _WRITE_TIMEOUT_S) -> str | None:
    """A reply draft in the person's style, or None to use the usual model.

    The caller already holds the foreground gate. One short-lived child loads the
    chat model's files with the adapter, writes, and exits. A draft that was cut
    off at the token limit, or that did not arrive within `timeout`, is not used.
    """
    if not ready() or timeout <= 1:
        return None
    python = mlx_python()
    base = _dir() / "base"
    if python is None or not (base / "config.json").exists():
        return None
    request = json.dumps({"model": str(base), "adapter": str(_adapter()), "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": material}]}).encode("utf-8")
    worker = Path(__file__).with_name("personal_style_worker.py")
    child = None
    try:
        child = await asyncio.create_subprocess_exec(
            *_command(python, script=worker), cwd=str(_dir()), env=_child_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(child.communicate(request), timeout=timeout)
        reply = json.loads(out.decode("utf-8"))
        text, finish = reply.get("text"), reply.get("finish")
    except BaseException as error:  # any failure means the usual model writes the draft
        if child is not None and child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.shield(asyncio.wait_for(child.wait(), timeout=5))
            except BaseException:  # noqa: BLE001
                pass
        if not isinstance(error, Exception):
            raise
        return None
    if finish != "stop" or not isinstance(text, str) or not text.strip():
        return None
    state = _state()
    state["drafts"] = int(state.get("drafts") or 0) + 1
    _save(state)
    return text.strip()
