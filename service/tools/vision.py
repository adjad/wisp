"""Vision tools — let the tool-driving agent 'see' via the vision model.

These bridge the agent model (gpt-oss, no vision) to the VLM (Qwen3-VL): capture
or load an image, swap to the vision model to describe it, then swap back so the
agent loop can continue.
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from service.config import agent_model, role_to_model
from service.inference.omlx_client import OMLXClient
from service.tools.registry import register


async def _describe(image_path: str, prompt: str) -> str:
    data = base64.b64encode(Path(image_path).read_bytes()).decode()
    url = f"data:image/png;base64,{data}"
    c = OMLXClient()
    try:
        await c.ensure_only(role_to_model("vision"))
        resp = await c.chat(
            role_to_model("vision"),
            [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            max_tokens=500,
        )
        desc = resp["choices"][0]["message"].get("content") or "(no description)"
        await c.ensure_only(agent_model())  # restore the agent model for the loop
        return desc
    finally:
        await c.aclose()


@register(
    "see_screen",
    "Capture the user's screen and describe what is visible, using the vision "
    "model. Use when the user asks about what is on their screen.",
    {"type": "object",
     "properties": {"question": {"type": "string",
                                 "description": "what to look for (optional)"}}},
    category="screen",
)
async def see_screen(question: str = "") -> str:
    tmp = os.path.join(tempfile.gettempdir(), f"moe_screen_{uuid.uuid4().hex}.png")
    try:
        subprocess.run(["screencapture", "-x", tmp], timeout=15, check=False)
    except Exception as e:  # noqa: BLE001
        return f"(could not capture screen: {e})"
    if not os.path.exists(tmp):
        return "(screen capture failed — Screen Recording permission may be needed)"
    try:
        return await _describe(tmp, question or "Describe what is on this screen.")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@register(
    "describe_image",
    "Describe or answer a question about an image file on disk, using the "
    "vision model.",
    {"type": "object",
     "properties": {"path": {"type": "string"},
                    "question": {"type": "string"}},
     "required": ["path"]},
    category="fs_read",
)
async def describe_image(path: str, question: str = "") -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such image: {p})"
    return await _describe(str(p), question or "Describe this image.")
