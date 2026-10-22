"""Terminal QR rendering, with no third-party dependency.

A small Swift script (qr.swift, CoreImage's CIQRCodeGenerator) produces the
module matrix; this module turns it into half-block characters. The code is
drawn dark-on-light (black modules on a white field, with a quiet zone) so a
phone can scan it off a dark terminal.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name("qr.swift")
QUIET_ZONE = 4
_FG_BLACK_BG_WHITE = "\x1b[38;5;16;48;5;231m"
_RESET = "\x1b[0m"


def matrix_from_output(text: str) -> list[list[bool]] | None:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if not rows or any(not re.fullmatch(r"[01]+", r) for r in rows):
        return None
    if len({len(r) for r in rows}) != 1 or len(rows) != len(rows[0]):
        return None
    matrix = [[c == "1" for c in r] for r in rows]
    # CoreImage pads the symbol with a light border; we add our own quiet zone.
    while len(matrix) > 1 and not any(matrix[0]) and not any(matrix[-1]) \
            and not any(r[0] for r in matrix) and not any(r[-1] for r in matrix):
        matrix = [r[1:-1] for r in matrix[1:-1]]
    return matrix


def render_halfblocks(matrix: list[list[bool]], quiet: int = QUIET_ZONE, color: bool = True) -> str:
    """Two module rows per text line. True = dark module."""
    size = len(matrix) + 2 * quiet
    grid = [[False] * size for _ in range(quiet)]
    for row in matrix:
        grid.append([False] * quiet + list(row) + [False] * quiet)
    grid += [[False] * size for _ in range(quiet)]
    if len(grid) % 2:
        grid.append([False] * size)
    lines = []
    for y in range(0, len(grid), 2):
        top, bottom = grid[y], grid[y + 1]
        chars = []
        for x in range(size):
            t, b = top[x], bottom[x]
            if color:  # black glyph on a white cell
                chars.append("█" if t and b else "▀" if t else "▄" if b else " ")
            else:  # no colour available: light modules are the filled glyphs
                chars.append(" " if t and b else "▄" if t else "▀" if b else "█")
        line = "".join(chars)
        lines.append(f"{_FG_BLACK_BG_WHITE}{line}{_RESET}" if color else line)
    return "\n".join(lines)


def render_qr(text: str, *, color: bool = True, timeout: float = 60.0) -> str | None:
    """The QR as terminal text, or None when the Swift toolchain is unavailable."""
    try:
        proc = subprocess.run(
            ["/usr/bin/xcrun", "swift", str(SCRIPT)], input=text + "\n",
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    matrix = matrix_from_output(proc.stdout)
    return render_halfblocks(matrix, color=color) if matrix else None
