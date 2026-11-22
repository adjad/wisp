"""Coding harnesses behind one small interface, and the Pi adapter (PLAN.md D9).

The runner (runner.py) only knows `HarnessAdapter` and `HarnessSession`:

- `HarnessAdapter.discover()` finds an install (or says why there is none);
- `HarnessAdapter.prepare()` writes the harness's private config for one project and
  returns the argv/env/cwd to start it with (the runner wraps the argv in sandbox-exec);
- `HarnessAdapter.session(proc)` turns the started process into a `HarnessSession`,
  which yields neutral `HarnessEvent`s and takes prompts, aborts and gate answers.

OpenCode could be added as a second adapter (its `serve` HTTP + SSE API maps onto the same
session calls: prompt -> POST /api/session/{id}/prompt, abort -> interrupt, gates ->
permission.asked / reply); only Pi is implemented.

Pi runs as `pi --mode rpc` (stdio JSONL). Its config dir is per project
(PI_CODING_AGENT_DIR), it is offline (PI_OFFLINE, PI_SKIP_VERSION_CHECK, PI_TELEMETRY=0,
--offline), every discovered extension, skill, theme, prompt template, MCP server and
context file is off, and it gets a short system prompt and four tools (config.py: the
engine's prefill guard leaves room for about 1.4k prompt tokens). The one extension it
loads is Wisp's gate (GATE_EXTENSION): before every tool call it asks the host over the
RPC channel (an `input` dialog titled GATE_TITLE) and blocks unless the host says yes; a
timeout or any error blocks too. It also cuts long tool results to TOOL_RESULT_CHARS.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from service.ideate import config, store

GATE_TITLE = "wisp-ideate-gate"
ENGINE_KEY_ENV = "WISP_IDEATE_ENGINE_KEY"
PI_ENTRY = Path("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")
BUNDLE_DIR = Path("Contents/Resources/ideate")


@dataclass
class HarnessInstall:
    name: str
    node: Path
    entry: Path
    version: str
    source: str                       # bundled | env | user
    # Folders the harness reads its own code from (granted read access in the sandbox).
    roots: list[Path] = field(default_factory=list)


@dataclass
class EngineTarget:
    base_url: str                     # http://127.0.0.1:<port>/v1
    port: int
    api_key: str = ""


@dataclass
class SpawnSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: Path


@dataclass
class HarnessEvent:
    kind: str                         # text thinking tool_start tool_end gate error settled exit
    text: str = ""
    tool: str = ""
    call_id: str = ""
    args: dict = field(default_factory=dict)
    gate_id: str = ""
    ok: bool = True
    aborted: bool = False
    stop_reason: str = ""
    returncode: int | None = None


class HarnessSession(ABC):
    @abstractmethod
    def events(self) -> Iterator[HarnessEvent]: ...

    @abstractmethod
    def prompt(self, text: str) -> None: ...

    @abstractmethod
    def abort(self) -> None: ...

    @abstractmethod
    def answer_gate(self, gate_id: str, allow: bool, reason: str = "") -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class HarnessAdapter(ABC):
    name = ""

    @abstractmethod
    def discover(self) -> tuple[HarnessInstall | None, str]: ...

    @abstractmethod
    def prepare(self, project: store.Project, install: HarnessInstall, engine: EngineTarget, *,
                kind: str, resume: bool) -> SpawnSpec: ...

    @abstractmethod
    def session(self, proc: subprocess.Popen) -> HarnessSession: ...


# ------------------------------------------------------------------------------ prompts ----

SYSTEM_PROMPT = """You are Ideate, a small coding agent inside Wisp on a Mac. You build one small project in the current folder.
Rules:
- Work only in the current folder. Use relative paths.
- Plan in one or two short lines, then act. Keep files small and plain.
- Use write for new files and edit for small changes. Use bash to list, run and test.
- To wait or poll, never repeat checks yourself: run ideate-loop --until "<cmd>" [--do "<cmd>"] [--every 2] [--max 120]. It returns one short summary.
- There is no internet: do not install packages; use what is already installed (python3, node, the standard library, plain HTML/CSS/JS).
- Every shell command may need the person's OK; a refused step is final, so choose another way or stop.
- Check your work once by running it, then stop with one or two sentences on what you made and how to open or run it."""

KIND_PROMPTS = {
    "general": "Start with the smallest version that works.",
    "ui": "Make a single index.html (inline CSS and JS is fine) that opens in a browser. Keep it tidy and readable.",
    "debug": "Reproduce the problem first, read the code before editing, then make the smallest fix and show it passes.",
    "learn": "Go in small steps. Explain each step in one plain sentence. Write tests and run them so the result is visible.",
    "script": "Write one script that runs as is. Make a small sample input and run the script on it.",
}


LOOP_TOOL = "ideate-loop"


def loop_runner_source() -> str:
    """loop.py as a standalone executable (it imports only the standard library)."""
    text = (Path(__file__).with_name("loop.py")).read_text(encoding="utf-8")
    return "#!/usr/bin/env python3\n" + text


def system_prompt(kind: str) -> str:
    return SYSTEM_PROMPT + "\n" + KIND_PROMPTS.get(kind, KIND_PROMPTS["general"])


GATE_EXTENSION = r"""// Written by Wisp for Ideate. Wisp rewrites this file before every start.
// Asks Wisp before every tool call; anything but an explicit yes blocks the call.
const TITLE = %(title)s;
const TIMEOUT_MS = %(timeout_ms)d;
const RESULT_CHARS = %(result_chars)d;
const KEY_ENV = %(key_env)s;
const BASH_TIMEOUT_S = %(bash_timeout_s)d;
const BASH_TIMEOUT_MAX_S = %(bash_timeout_max_s)d;
const LOOP_TIMEOUT_S = %(loop_timeout_s)d;
const LOOP_TOOL = %(loop_tool)s;

// Every bash call gets a time limit: the agent's own if smaller, never more than the max.
function bashTimeout(input) {
  const asked = typeof input.timeout === "number" && isFinite(input.timeout) && input.timeout > 0
    ? input.timeout : BASH_TIMEOUT_S;
  const cap = typeof input.command === "string" && input.command.indexOf(LOOP_TOOL) !== -1
    ? LOOP_TIMEOUT_S : BASH_TIMEOUT_MAX_S;
  return Math.min(asked, cap);
}

// Command output: the tail matters (the error, the last test line), so keep the end.
function tail(text, limit) {
  if (typeof text !== "string" || text.length <= limit) return text;
  return "[... " + (text.length - limit) + " characters cut ...]\n" + text.slice(-limit);
}

// Pi's timeout result ("Command timed out after N seconds") becomes one line plus the last lines.
function bashResult(text) {
  const m = typeof text === "string" ? text.match(/Command timed out after (\d+(?:\.\d+)?) seconds/) : null;
  if (m) {
    const rest = text.replace(m[0], "").trim();
    const lead = "stopped after " + m[1] + " s, last lines:\n";
    return lead + tail(rest, RESULT_CHARS - lead.length);
  }
  return tail(text, RESULT_CHARS);
}

function shorten(text) {
  if (typeof text !== "string" || text.length <= RESULT_CHARS) return text;
  const half = Math.floor(RESULT_CHARS / 2);
  return text.slice(0, half) + "\n[... " + (text.length - RESULT_CHARS) + " characters cut ...]\n" + text.slice(-half);
}

export default function (pi: any) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    let raw: any = undefined;
    try {
      const ask = JSON.stringify({ id: event.toolCallId, tool: event.toolName, input: event.input });
      raw = await ctx.ui.input(TITLE, ask, { timeout: TIMEOUT_MS });
    } catch (e) {
      return { block: true, reason: "Wisp could not check this step, so it did not run." };
    }
    let answer: any = null;
    try { answer = typeof raw === "string" ? JSON.parse(raw) : null; } catch (e) { answer = null; }
    if (!answer || answer.allow !== true) {
      const why = answer && typeof answer.reason === "string" && answer.reason ? answer.reason
        : "Not allowed: no answer came in time.";
      return { block: true, reason: why };
    }
    if (event.toolName === "bash" && event.input && typeof event.input.command === "string") {
      event.input.timeout = bashTimeout(event.input);
      event.input.command = "unset " + KEY_ENV + "; " + event.input.command;
    }
    return undefined;
  });
  pi.on("tool_result", async (event: any) => {
    if (!Array.isArray(event.content)) return undefined;
    let changed = false;
    const isBash = event.toolName === "bash";
    const content = event.content.map((part: any) => {
      if (!part || part.type !== "text" || typeof part.text !== "string") return part;
      const text = isBash ? bashResult(part.text) : part.text.length > RESULT_CHARS ? shorten(part.text) : part.text;
      if (text !== part.text) { changed = true; return { ...part, text }; }
      return part;
    });
    return changed ? { content } : undefined;
  });
}
"""


def gate_extension() -> str:
    return GATE_EXTENSION % {
        "title": json.dumps(GATE_TITLE), "key_env": json.dumps(ENGINE_KEY_ENV),
        "timeout_ms": int((config.APPROVAL_TIMEOUT_S + 30) * 1000),
        "result_chars": int(config.TOOL_RESULT_CHARS),
        "bash_timeout_s": int(config.BASH_TIMEOUT_S), "bash_timeout_max_s": int(config.BASH_TIMEOUT_MAX_S),
        "loop_timeout_s": int(config.LOOP_BASH_TIMEOUT_S), "loop_tool": json.dumps(LOOP_TOOL),
    }


# --------------------------------------------------------------------------- discovery ----

def _node_candidates() -> list[Path]:
    home = Path.home()
    found = []
    if os.environ.get("WISP_IDEATE_NODE"):
        found.append(Path(os.environ["WISP_IDEATE_NODE"]).expanduser())
    found += [home / ".local/share/pi-node/current/bin/node", Path("/opt/homebrew/bin/node"),
              Path("/usr/local/bin/node")]
    return found


def _first_node() -> Path | None:
    for candidate in _node_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _release_entry(path: Path) -> tuple[Path, str] | None:
    """(cli.js, version) for a Pi release folder, a Pi install root (~/.pi/agent) or cli.js itself."""
    path = path.expanduser()
    if path.is_file() and path.name == "cli.js":
        package = path.parents[2] / "package.json"
        return path, _package_version(package)
    if (path / "install" / "current-version").is_file():
        version = (path / "install" / "current-version").read_text().strip()
        if not version or "/" in version or version.startswith("."):
            return None
        path = path / "install" / "releases" / version
    entry = path / PI_ENTRY
    if entry.is_file():
        return entry, _package_version(entry.parents[2] / "package.json")
    return None


def _package_version(package: Path) -> str:
    try:
        return str(json.loads(package.read_text()).get("version") or "")
    except (OSError, ValueError):
        return ""


class PiAdapter(HarnessAdapter):
    name = "pi"

    def discover(self) -> tuple[HarnessInstall | None, str]:
        from service.safety.shell_sandbox import app_bundle
        bundle = app_bundle()
        if bundle is not None:
            root = bundle / BUNDLE_DIR
            node, entry = root / "node" / "bin" / "node", root / "pi" / PI_ENTRY
            if node.is_file() and entry.is_file():
                version = _package_version(entry.parents[2] / "package.json")
                return HarnessInstall("pi", node, entry, version, "bundled", [root]), ""
            if not os.environ.get("WISP_IDEATE_PI"):
                return None, "Ideate's coding agent isn't included in this build of Wisp."
        override = os.environ.get("WISP_IDEATE_PI")
        if override:
            found = _release_entry(Path(override))
            node = _first_node()
            if found is None or node is None:
                return None, "WISP_IDEATE_PI doesn't point at a Pi release with Node beside it."
            entry, version = found
            if len(entry.parents) < 6:
                return None, "WISP_IDEATE_PI doesn't point at a Pi release with Node beside it."
            return HarnessInstall("pi", node, entry, version, "env", [entry.parents[5], node.parent]), ""
        found = _release_entry(Path.home() / ".pi" / "agent")
        node = _first_node()
        if found is None:
            return None, "Ideate's coding agent (Pi) isn't installed on this Mac."
        if node is None:
            return None, "Ideate's coding agent needs Node.js 22 or newer, which isn't installed."
        entry, version = found
        if len(entry.parents) < 6:
            return None, "Ideate's coding agent (Pi) isn't installed in the expected place."
        return HarnessInstall("pi", node, entry, version, "user", [entry.parents[5], node.parent]), ""

    def prepare(self, project: store.Project, install: HarnessInstall, engine: EngineTarget, *,
                kind: str, resume: bool) -> SpawnSpec:
        agent = project.agent
        pi_dir, sessions, home, tmp = agent / "pi", agent / "sessions", agent / "home", agent / "tmp"
        xdg = agent / "xdg"
        for folder in (pi_dir, sessions, home, tmp, xdg / "config", xdg / "data", xdg / "cache", xdg / "state"):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        models = {"providers": {config.PROVIDER: {
            "baseUrl": engine.base_url, "api": "openai-completions",
            "apiKey": ("$" + ENGINE_KEY_ENV) if engine.api_key else "local",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True,
                       "thinkingFormat": "openai", "supportsUsageInStreaming": True,
                       "maxTokensField": "max_tokens"},
            "models": [{"id": config.MODEL_ID, "name": config.MODEL_LABEL, "reasoning": True,
                        "input": ["text"], "contextWindow": config.CONTEXT_WINDOW,
                        "maxTokens": config.MAX_OUTPUT_TOKENS}]}}}
        settings = {"defaultProvider": config.PROVIDER, "defaultModel": config.MODEL_ID,
                    "enableInstallTelemetry": False,
                    "compaction": {"enabled": True, "reserveTokens": config.COMPACT_RESERVE_TOKENS,
                                   "keepRecentTokens": config.COMPACT_KEEP_RECENT_TOKENS}}
        # Rewritten before every start: whatever the harness left in its own dir is replaced.
        for name, value in (("models.json", models), ("settings.json", settings)):
            _write_private(pi_dir / name, json.dumps(value, indent=2))
        for stale in ("auth.json",):
            (pi_dir / stale).unlink(missing_ok=True)
        # Wisp-owned, outside every folder the sandbox lets the harness write.
        gate = project.private / "ideate-gate.ts"
        prompt = project.private / "system-prompt.md"
        _write_private(gate, gate_extension())
        _write_private(prompt, system_prompt(kind))
        loop_bin = project.private / "bin"
        loop_bin.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(loop_bin / LOOP_TOOL, loop_runner_source())
        (loop_bin / LOOP_TOOL).chmod(0o700)
        work = str(project.work)
        argv = [str(install.node), str(install.entry), "--mode", "rpc", "--offline",
                "--provider", config.PROVIDER, "--model", config.MODEL_ID,
                "--thinking", config.THINKING_LEVEL, "--session-dir", str(sessions),
                "--tools", ",".join(config.TOOLS), "--system-prompt", str(prompt),
                "-ne", "-e", str(gate), "--no-mcp", "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-context-files", "--no-approve"]
        if resume:
            argv.append("--continue")
        path = os.pathsep.join(dict.fromkeys([str(loop_bin), str(install.node.parent), "/usr/bin", "/bin", "/usr/sbin",
                                              "/sbin", "/opt/homebrew/bin", "/usr/local/bin"]))
        env = {"HOME": str(home), "PATH": path, "TMPDIR": str(tmp) + "/", "PWD": work,
               "LANG": "en_US.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
               "XDG_CONFIG_HOME": str(xdg / "config"), "XDG_DATA_HOME": str(xdg / "data"),
               "XDG_CACHE_HOME": str(xdg / "cache"), "XDG_STATE_HOME": str(xdg / "state"),
               "PI_CODING_AGENT_DIR": str(pi_dir), "PI_CODING_AGENT_SESSION_DIR": str(sessions),
               "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
               "npm_config_cache": str(xdg / "cache" / "npm"), "PYTHONDONTWRITEBYTECODE": "1"}
        if engine.api_key:
            env[ENGINE_KEY_ENV] = engine.api_key
        return SpawnSpec(argv, env, project.work)

    def session(self, proc: subprocess.Popen) -> HarnessSession:
        return PiSession(proc)


def _write_private(path: Path, text: str) -> None:
    from service import paths
    paths.private_write_bytes(path, text.encode("utf-8"))


# ----------------------------------------------------------------------------- session ----

class PiSession(HarnessSession):
    """Pi's RPC JSONL, translated. Lines are split on LF only (never on U+2028)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._write = threading.Lock()
        self._counter = 0
        self._thinking_in_message = False

    def _send(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._write:
            stdin = self.proc.stdin
            if stdin is None or stdin.closed:
                return
            try:
                stdin.write(line.encode("utf-8"))
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _next_id(self) -> str:
        self._counter += 1
        return f"w{self._counter}"

    def prompt(self, text: str) -> None:
        self._send({"id": self._next_id(), "type": "prompt", "message": text})

    def abort(self) -> None:
        self._send({"id": self._next_id(), "type": "abort"})

    def answer_gate(self, gate_id: str, allow: bool, reason: str = "") -> None:
        value = json.dumps({"allow": bool(allow), "reason": reason})
        self._send({"type": "extension_ui_response", "id": gate_id, "value": value})

    def close(self) -> None:
        with self._write:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except OSError:
                pass

    def events(self) -> Iterator[HarnessEvent]:
        stdout = self.proc.stdout
        assert stdout is not None
        buffer = b""
        while True:
            chunk = stdout.read1(65536) if hasattr(stdout, "read1") else stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield from self._translate_line(line)
            if len(buffer) > 16 * 1024 * 1024:       # a runaway line: drop it
                buffer = b""
        if buffer.strip():
            yield from self._translate_line(buffer)
        yield HarnessEvent("exit", returncode=self.proc.wait())

    def _translate_line(self, line: bytes) -> Iterator[HarnessEvent]:
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if isinstance(record, dict):
            yield from self.translate(record)

    def translate(self, record: dict) -> Iterator[HarnessEvent]:
        kind = record.get("type")
        if kind == "extension_ui_request":
            method = record.get("method")
            if method == "input" and record.get("title") == GATE_TITLE:
                try:
                    ask = json.loads(record.get("placeholder") or "{}")
                except ValueError:
                    ask = {}
                ask = ask if isinstance(ask, dict) else {}
                args = ask.get("input") if isinstance(ask.get("input"), dict) else {}
                yield HarnessEvent("gate", gate_id=str(record.get("id") or ""), tool=str(ask.get("tool") or ""),
                                   call_id=str(ask.get("id") or ""), args=args)
            elif method in ("select", "confirm", "input", "editor"):
                # Nothing else may ask through this channel: answer "cancelled" (fail closed).
                self._send({"type": "extension_ui_response", "id": record.get("id"), "cancelled": True})
            return
        if kind == "message_start":
            self._thinking_in_message = False
            return
        if kind == "message_update":
            sub = (record.get("assistantMessageEvent") or {}).get("type", "")
            if sub.startswith("thinking") and not self._thinking_in_message:
                self._thinking_in_message = True
                yield HarnessEvent("thinking")
            return
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") != "assistant":
                return
            content = message.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            texts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            if not self._thinking_in_message and any(isinstance(p, dict) and p.get("type") == "thinking"
                                                     for p in content):
                yield HarnessEvent("thinking")
            self._thinking_in_message = False
            text = "\n".join(t for t in texts if t).strip()
            if text:
                yield HarnessEvent("text", text=text)
            reason = str(message.get("stopReason") or "")
            if reason == "error":
                yield HarnessEvent("error", text=str(message.get("errorMessage") or ""), stop_reason=reason)
            return
        if kind == "tool_execution_start":
            args = record.get("args") if isinstance(record.get("args"), dict) else {}
            yield HarnessEvent("tool_start", tool=str(record.get("toolName") or ""),
                               call_id=str(record.get("toolCallId") or ""), args=args)
            return
        if kind == "tool_execution_end":
            yield HarnessEvent("tool_end", tool=str(record.get("toolName") or ""),
                               call_id=str(record.get("toolCallId") or ""), ok=not record.get("isError"))
            return
        if kind == "agent_settled":
            yield HarnessEvent("settled", aborted=bool(record.get("aborted")))
            return
        if kind == "response" and record.get("success") is False:
            yield HarnessEvent("error", text=str(record.get("error") or "The coding agent refused that request."),
                               stop_reason="response")
            return
        if kind == "response" and record.get("command") == "prompt":
            if (record.get("data") or {}).get("disposition") == "handled":
                yield HarnessEvent("settled", aborted=False)


ADAPTERS: dict[str, HarnessAdapter] = {"pi": PiAdapter()}


def default_adapter() -> HarnessAdapter:
    """Pi; WISP_IDEATE_HARNESS may name another registered adapter."""
    return ADAPTERS.get(os.environ.get("WISP_IDEATE_HARNESS", "pi"), ADAPTERS["pi"])
