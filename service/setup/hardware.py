"""What Mac Wisp is running on, so setup can recommend a model that fits."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

Runner = Callable[[list[str]], str]


def _sysctl(args: list[str]) -> str:
    return subprocess.run(["/usr/sbin/sysctl", "-n", *args], capture_output=True,
                          text=True, timeout=3, check=False).stdout.strip()


@dataclass(frozen=True)
class Hardware:
    chip: str
    ram_gb: int

    @property
    def tier(self) -> str:
        """`roomy` 32+ GB, `comfortable` 24-31, `standard` 16-23, `tight` below 16."""
        if self.ram_gb >= 32:
            return "roomy"
        if self.ram_gb >= 24:
            return "comfortable"
        if self.ram_gb >= 16:
            return "standard"
        return "tight"


def detect_hardware(run: Runner = _sysctl) -> Hardware:
    """Never raises: an unreadable value degrades to the most cautious tier."""
    try:
        ram_gb = round(int(run(["hw.memsize"])) / 2**30)
    except (ValueError, OSError, subprocess.SubprocessError):
        ram_gb = 0
    try:
        chip = run(["machdep.cpu.brand_string"]) or "Apple silicon"
    except (OSError, subprocess.SubprocessError):
        chip = "Apple silicon"
    return Hardware(chip=chip, ram_gb=max(ram_gb, 0))


_cached: Hardware | None = None


def current_hardware() -> Hardware:
    """Detected once per process: memory and chip do not change while Wisp runs.
    A failed read (ram 0) is not cached, so a transient error can recover."""
    global _cached
    if _cached is None:
        found = detect_hardware()
        if found.ram_gb <= 0:
            return found
        _cached = found
    return _cached
