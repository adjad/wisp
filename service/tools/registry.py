"""Tool registry. Each tool declares a safety `category` used by the policy engine."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

REGISTRY: dict[str, Tool] = {}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema for the arguments
    category: str             # safety category: shell, fs_read, fs_write, ...
    func: Callable[..., Any]  # called with **args, returns a string (sync or async)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def register(name: str, description: str, parameters: dict, category: str):
    def deco(func):
        REGISTRY[name] = Tool(name, description, parameters, category, func)
        return func
    return deco


def get_tool(name: str) -> Tool | None:
    return REGISTRY.get(name)


def tool_schemas(names: list[str] | None = None) -> list[dict]:
    tools = REGISTRY.values() if names is None else [REGISTRY[n] for n in names if n in REGISTRY]
    return [t.schema() for t in tools]


def _arg_hint(tool: Tool) -> str:
    """A compact "name: type (required)" listing for the tool's real
    parameters — fed back on a bad call so the model can self-correct instead
    of guessing again blind."""
    props = tool.parameters.get("properties", {})
    required = set(tool.parameters.get("required", []))
    parts = [f"{name}: {schema.get('type', 'any')}" + (" (required)" if name in required else "")
             for name, schema in props.items()]
    return ", ".join(parts) or "(no arguments)"


async def run_tool(tool: Tool, args: dict) -> str:
    """Runs the tool and returns its result as a tool_result string.

    Previously an unhandled exception here (missing/misnamed/wrong-typed
    argument -> TypeError, or any other runtime error) propagated all the way
    up through the agent loop and killed the ENTIRE turn with a generic
    top-level error — the model never saw what went wrong and had no chance
    to retry with corrected arguments, which is the actual fix for a bad tool
    call. Caught here instead, so a bad call becomes an ordinary tool_result
    the model can read and correct on its next step, the same way a wrong
    file path or an ambiguous title already does.
    """
    # Drop junk empty-name keys before dispatch. Small models emit `{"": ""}`
    # for a no-argument tool instead of `{}` — observed live: a `show_profile`
    # call came back as {'': ''}, raised TypeError, and burned a whole agent
    # step recovering from it. An empty key can never be a real parameter name,
    # so silently ignoring it is strictly better than failing the call.
    if args:
        args = {k: v for k, v in args.items() if k}
    try:
        res = tool.func(**args)
        if inspect.isawaitable(res):
            res = await res
        return str(res)
    except TypeError as e:
        return (f"(error calling {tool.name}({args!r}): {e}. "
                f"Expected arguments — {_arg_hint(tool)}. Call it again with corrected arguments.)")
    except Exception as e:  # noqa: BLE001 — must surface as a tool_result, never crash the turn
        return f"(error running {tool.name}: {e})"
