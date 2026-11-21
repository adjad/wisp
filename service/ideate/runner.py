"""Runs: one harness process at a time, its events, its approvals, cancel.

One run at a time across all projects: the engine serves one model and has no parallelism,
so a second start is refused with a clear line (`Busy`) rather than queued behind a run
that may wait on an approval for minutes.

A run, on its own thread:
1. checks the engine (Inkling listed? loaded?) and, when it is not loaded, says so in the
   first event and loads it through the engine client's swap helper (engine.py);
2. writes the harness's config and the sandbox profile, and starts the harness under
   sandbox-exec (never without it: no sandbox, no run);
3. sends the prompt and turns the harness's events into the tab's events;
4. answers every gate request with gate.judge(): allowed at once, refused with Wisp's
   reason, or held as a pending approval until the tab answers (a timeout is a no);
5. ends on the harness's "settled" marker (done, cancelled, or failed with a line why).
   When the engine's prefill guard refuses a step, the run starts once more in a fresh
   conversation that carries a short summary and the file list (config.FRESH_RESTARTS);
   after that it fails with config.GUARD_ERROR.
Cancel sends the harness's abort, then kills the process group after config.CANCEL_GRACE_S.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
from dataclasses import dataclass, field

from service.ideate import config, gate, harness, sandbox, store
from service.ideate import engine as engine_mod

log = logging.getLogger(__name__)

_GUARD = re.compile(r"(?i)memory|prefill|exceed|too (?:large|long)|insufficient|\b507\b|context.length|\bkv\b")


class Busy(RuntimeError):
    pass


class Unavailable(RuntimeError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class Pending:
    id: str
    gate_id: str
    call_id: str
    tool: str
    text: str
    path: str
    note: str
    reason: str
    timer: threading.Timer | None = None

    def public(self) -> dict:
        return {"id": self.id, "tool": self.tool, "text": self.text, "path": self.path,
                "note": self.note, "reason": self.reason}


@dataclass
class Run:
    project: store.Project
    prompt: str
    resume: bool
    install: harness.HarnessInstall
    engine: harness.EngineTarget
    kind: str
    session: harness.HarnessSession | None = None
    proc: subprocess.Popen | None = None
    cancel: bool = False
    pending: dict = field(default_factory=dict)
    denied: set = field(default_factory=set)
    calls: dict = field(default_factory=dict)
    texts: list = field(default_factory=list)
    error: str = ""
    guard: bool = False
    thread: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event)


class Manager:
    def __init__(self, store_: store.Store | None = None, adapter: harness.HarnessAdapter | None = None,
                 residency=None, *, sandbox_usable=None, engine_target=None, api_key=None,
                 popen=subprocess.Popen, contain=None, profile_builder=None):
        self.store = store_ or store.Store()
        self.adapter = adapter or harness.default_adapter()
        self.residency = residency or engine_mod.Residency()
        self._sandbox_usable = sandbox_usable or sandbox.usable
        self._engine_target = engine_target or engine_mod.target
        self._api_key = api_key or engine_mod.api_key
        self._popen = popen
        self._contain = contain or sandbox.contain
        self._profile = profile_builder or sandbox.build_profile
        self._lock = threading.RLock()
        self.active: Run | None = None

    # ------------------------------------------------------------------ availability ----
    def preflight(self) -> tuple[harness.HarnessInstall | None, harness.EngineTarget | None, str]:
        """(install, engine, reason). reason is "" when a run could start (model aside)."""
        install, why = self.adapter.discover()
        if install is None:
            return None, None, why
        if not self._sandbox_usable():
            return install, None, ("Ideate can't start here: Wisp can't put its coding agent in a "
                                   "sandbox on this Mac right now, and it never runs it without one.")
        try:
            target = self._engine_target()
        except engine_mod.EngineUnavailable as exc:
            return install, None, str(exc)
        return install, target, ""

    def busy_with(self) -> str | None:
        with self._lock:
            if self.active is None:
                return None
            try:
                return self.store.meta(self.active.project).get("name") or "another idea"
            except (OSError, ValueError):
                return "another idea"

    # ------------------------------------------------------------------------ starts ----
    def _refuse_if_busy(self) -> None:
        name = self.busy_with()
        if name is not None:
            raise Busy(f"Ideate is already working on {name}. Wait for it to finish, or stop it first.")

    def start_new(self, prompt: str, kind: str, name: str | None = None) -> store.Project:
        with self._lock:
            self._refuse_if_busy()
            install, target, why = self.preflight()
            if why:
                raise Unavailable(why)
            project = self.store.create(prompt, kind, name)
            self._launch(project, prompt, False, install, target)
            return project

    def message(self, project_id: str, text: str) -> store.Project:
        with self._lock:
            project = self._project(project_id)
            self._refuse_if_busy()
            install, target, why = self.preflight()
            if why:
                raise Unavailable(why)
            self.store.recover(project, running=False)
            self.store.update(project, last_message=store.cap(text, config.PROMPT_CHARS))
            self._launch(project, text, True, install, target)
            return project

    def _project(self, project_id: str) -> store.Project:
        try:
            return self.store.project(project_id)
        except store.ProjectNotFound:
            raise NotFound(project_id) from None

    def _launch(self, project, text, resume, install, target) -> None:
        kind = self.store.meta(project).get("kind") or "general"
        run = Run(project, text, resume, install, target, kind)
        self.active = run
        self.store.update(project, status="starting")
        run.thread = threading.Thread(target=self._drive, args=(run,), name=f"ideate-{project.id}", daemon=True)
        run.thread.start()

    # ------------------------------------------------------------------------- drive ----
    def _drive(self, run: Run) -> None:
        project = run.project
        begun = False
        outcome = "failed"
        try:
            try:
                state = self.residency.state_sync()
            except Exception:  # noqa: BLE001
                state = engine_mod.ModelState(False, False, False)
            if not state.reachable:
                run.error = "Wisp's engine isn't answering. Try again in a moment."
                return
            if not state.listed:
                run.error = (f"{config.MODEL_LABEL} isn't downloaded yet. Open Settings and download it, "
                             "then come back.")
                return
            self.residency.begin()
            begun = True
            if not state.loaded:
                self.store.append(project, "text", text=f"Loading {config.MODEL_LABEL}. Ask's model steps "
                                                       "aside while Ideate works, and comes back after.")
                try:
                    self.residency.ensure_sync()
                except Exception as exc:  # noqa: BLE001
                    log.info("ideate: model load failed: %s", type(exc).__name__)
                    run.error = f"{config.MODEL_LABEL} couldn't load. Close other apps to free memory, then try again."
                    return
            if run.cancel:
                outcome = "cancelled"
                return
            prompt, resume, restarts = run.prompt, run.resume, 0
            while True:
                outcome = self._session(run, prompt, resume)
                if outcome == "guard" and restarts < config.FRESH_RESTARTS and not run.cancel:
                    restarts += 1
                    self.store.append(project, "text", text="That step was too big for the model's memory room. "
                                                           "Starting fresh with a short summary.")
                    prompt, resume = self._carry_over(run), False
                    run.texts, run.guard, run.error = [], False, ""
                    continue
                break
        except Exception:  # noqa: BLE001 - the tab must hear about it, never a stuck run
            log.exception("ideate run failed")
            run.error = "Ideate stopped unexpectedly. Your files are kept."
            outcome = "failed"
        finally:
            self._finish(run, outcome, begun)

    def _finish(self, run: Run, outcome: str, begun: bool) -> None:
        project = run.project
        with self._lock:
            for pending in list(run.pending.values()):
                if pending.timer:
                    pending.timer.cancel()
            run.pending.clear()
        try:
            if outcome == "done":
                self.store.append(project, "done")
                self.store.update(project, status="done")
            elif outcome == "cancelled" or run.cancel:
                self.store.append(project, "error", text="Stopped. Your files are kept.")
                self.store.update(project, status="cancelled")
            else:
                text = config.GUARD_ERROR if outcome == "guard" else (run.error or "Ideate stopped with an error.")
                self.store.append(project, "error", text=text)
                self.store.update(project, status="failed")
        finally:
            if begun:
                try:
                    self.residency.end()
                except Exception:  # noqa: BLE001
                    log.exception("ideate: releasing the model failed")
            with self._lock:
                if self.active is run:
                    self.active = None
            run.done.set()

    def _carry_over(self, run: Run) -> str:
        said = store.cap(" ".join(run.texts[-3:]), config.CARRY_SUMMARY_CHARS) or "nothing yet"
        names = [row["path"] for row in self.store.files(run.project, limit=30)]
        return ("Continue this task from a fresh start (the earlier conversation got too long for the model).\n"
                f"Task: {store.cap(run.prompt, 1200)}\n"
                f"Where it got to: {said}\n"
                f"Files in the folder: {', '.join(names) if names else 'none yet'}\n"
                "Pick up from there in small steps.")

    def _session(self, run: Run, prompt: str, resume: bool) -> str:
        project = run.project
        spec = self.adapter.prepare(project, run.install,
                                    harness.EngineTarget(run.engine.base_url, run.engine.port, self._api_key()),
                                    kind=run.kind, resume=resume)
        profile_path = project.private / "sandbox.sb"
        from service import paths
        paths.private_write_bytes(profile_path, self._profile(
            project.root, [project.work, project.agent], run.engine.port,
            harness_roots=list(run.install.roots)).encode())
        argv = self._contain(spec.argv, profile_path)
        try:
            if project.log_path.exists() and project.log_path.stat().st_size > config.HARNESS_LOG_BYTES:
                project.log_path.unlink()
        except OSError:
            pass
        log_fd = os.open(project.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            proc = self._popen(argv, cwd=str(spec.cwd), env=spec.env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log_fd, start_new_session=True)
        finally:
            os.close(log_fd)
        session = self.adapter.session(proc)
        with self._lock:
            run.proc, run.session = proc, session
        turns = int(self.store.meta(project).get("turns") or 0) + 1
        self.store.update(project, status="running", turns=turns)
        outcome = None
        try:
            if run.cancel:
                session.abort()
            session.prompt(prompt)
            for event in session.events():
                if event.kind == "settled":
                    if event.aborted or run.cancel:
                        outcome = "cancelled"
                    elif run.guard:
                        outcome = "guard"
                    elif run.error:
                        outcome = "failed"
                    else:
                        outcome = "done"
                    session.close()
                elif event.kind == "exit":
                    if outcome is None:
                        if run.cancel:
                            outcome = "cancelled"
                        elif run.guard:
                            outcome = "guard"
                        else:
                            run.error = run.error or "The coding agent stopped unexpectedly. Your files are kept."
                            outcome = "failed"
                    break
                else:
                    self._event(run, event)
        finally:
            with self._lock:
                for pending in list(run.pending.values()):
                    if pending.timer:
                        pending.timer.cancel()
                run.pending.clear()
            session.close()
            _reap(proc)
            with self._lock:
                run.proc, run.session = None, None
        return outcome or "failed"

    # ------------------------------------------------------------------------ events ----
    def _event(self, run: Run, event: harness.HarnessEvent) -> None:
        project, work = run.project, run.project.work
        if event.kind == "thinking":
            self.store.append(project, "thinking")
        elif event.kind == "text":
            run.texts.append(event.text)
            run.error, run.guard = "", False       # a later answer means a retry got through
            self.store.append(project, "text", text=event.text)
        elif event.kind == "error":
            if _GUARD.search(event.text or ""):
                run.guard = True
            else:
                first = store.cap((event.text or "").strip().splitlines()[0] if (event.text or "").strip() else "", 160)
                run.error = "The model stopped with an error" + (f": {first}" if first else ".")
        elif event.kind == "tool_start":
            run.error, run.guard = "", False
            text, path = _describe(event.tool, event.args, work)
            run.calls[event.call_id] = (event.tool, text, path)
            self.store.append(project, "tool", tool=event.tool, text=text, path=path, status="running")
        elif event.kind == "tool_end":
            if event.call_id in run.denied:
                return
            tool, text, path = run.calls.get(event.call_id, (event.tool, "", ""))
            self.store.append(project, "tool", tool=tool, text=text, path=path,
                              status="ok" if event.ok else "error")
            if event.ok and tool in ("write", "edit") and path:
                self.store.append(project, "file", path=path)
        elif event.kind == "gate":
            self._gate(run, event)

    def _gate(self, run: Run, event: harness.HarnessEvent) -> None:
        session = run.session
        if session is None:
            return
        if run.cancel:
            session.answer_gate(event.gate_id, False, "Stopped.")
            return
        port = run.engine.port if run.engine is not None else None
        verdict = gate.judge(event.tool, event.args, run.project.work, port)
        if verdict.action == gate.ALLOW:
            session.answer_gate(event.gate_id, True)
            return
        text, path = _describe(event.tool, event.args, run.project.work)
        if verdict.action == gate.DENY:
            session.answer_gate(event.gate_id, False, f"Not allowed by Wisp: {verdict.reason}")
            self._denied(run, event.call_id, event.tool, text, path)
            return
        pending = Pending(id=secrets.token_hex(6), gate_id=event.gate_id, call_id=event.call_id,
                          tool=event.tool, text=text, path=path, note=verdict.note, reason=verdict.reason)
        timer = threading.Timer(config.APPROVAL_TIMEOUT_S, self._timeout, args=(run, pending.id))
        timer.daemon = True
        pending.timer = timer
        with self._lock:
            run.pending[pending.id] = pending
        self.store.append(run.project, "approval", tool=event.tool, text=text or path, path=path, status="waiting")
        self.store.update(run.project, status="waiting")
        timer.start()

    def _denied(self, run: Run, call_id: str, tool: str, text: str, path: str) -> None:
        run.denied.add(call_id)
        self.store.append(run.project, "tool", tool=tool, text=text, path=path, status="denied")

    def _timeout(self, run: Run, approval_id: str) -> None:
        self._resolve(run, approval_id, False, "No answer came in time, so Wisp said no.")

    def _resolve(self, run: Run, approval_id: str, allow: bool, reason: str) -> bool:
        with self._lock:
            pending = run.pending.pop(approval_id, None)
            session = run.session
            left = len(run.pending)
        if pending is None:
            return False
        if pending.timer:
            pending.timer.cancel()
        if session is not None:
            session.answer_gate(pending.gate_id, allow, "" if allow else reason)
        if not allow:
            self._denied(run, pending.call_id, pending.tool, pending.text, pending.path)
        if not left and not run.cancel and self.active is run:
            self.store.update(run.project, status="running")
        return True

    # ----------------------------------------------------------------- tab's actions ----
    def answer(self, project_id: str, approval_id: str, allow: bool) -> None:
        self._project(project_id)
        with self._lock:
            run = self.active
        if run is None or run.project.id != project_id or not self._resolve(
                run, approval_id, bool(allow), "The person said no to this step."):
            raise NotFound(approval_id)

    def cancel(self, project_id: str) -> None:
        project = self._project(project_id)
        with self._lock:
            run = self.active
            if run is None or run.project.id != project_id:
                self.store.recover(project, running=False)
                return
            run.cancel = True
            pending_ids = list(run.pending)
            session, proc = run.session, run.proc
        for approval_id in pending_ids:
            self._resolve(run, approval_id, False, "Stopped.")
        if session is not None:
            session.abort()
        if proc is not None:
            killer = threading.Timer(config.CANCEL_GRACE_S, _kill_group, args=(proc,))
            killer.daemon = True
            killer.start()

    def view(self, project_id: str, after: int = 0) -> dict:
        project = self._project(project_id)
        with self._lock:
            run = self.active if self.active and self.active.project.id == project_id else None
            approvals = [p.public() for p in run.pending.values()] if run else []
        self.store.recover(project, running=run is not None)
        meta = self.store.meta(project)
        out = store.summary(meta)
        out["path"] = str(project.work)
        out["events"] = self.store.events(project, after=max(0, int(after or 0)))
        out["files"] = self.store.files(project)
        out["approvals"] = approvals
        return out

    def shutdown(self) -> None:
        with self._lock:
            run = self.active
        if run is not None:
            run.cancel = True
            if run.proc is not None:
                _kill_group(run.proc)


def _describe(tool: str, args: dict, work) -> tuple[str, str]:
    """(text, path) for an event: the command for bash, the project-relative path for files."""
    args = args if isinstance(args, dict) else {}
    if tool == "bash":
        return store.cap(args.get("command") or "", config.COMMAND_TEXT_CHARS), ""
    raw = args.get("path")
    return "", store.relative_display(work, raw) if isinstance(raw, str) and raw else ""


def _kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=config.CANCEL_GRACE_S)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
    for stream in (proc.stdout, proc.stdin):
        try:
            if stream:
                stream.close()
        except OSError:
            pass


_manager: Manager | None = None
_manager_lock = threading.Lock()


def manager() -> Manager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = Manager()
            atexit.register(_manager.shutdown)
        return _manager
