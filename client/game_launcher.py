"""
game_launcher.py – launch TLauncher and track the Minecraft javaw process.

Windows-only in production; psutil is used for process detection.
The design is intentionally thin so tests can substitute fake processes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:  # graceful degradation for CI / non-Windows
    psutil = None  # type: ignore[assignment]

# ------------------------------------------------------------------
# TLauncher discovery
# ------------------------------------------------------------------

import os

_APPDATA = Path(os.environ.get("APPDATA", Path.home()))

TLAUNCHER_CANDIDATES: list[Path] = [
    _APPDATA / "TLauncher" / "TLauncher.exe",
    _APPDATA / "TLauncher" / "TLauncher.jar",
    _APPDATA / ".tlauncher" / "TLauncher.jar",
    Path.home() / "TLauncher" / "TLauncher.exe",
]

# How long to wait for javaw.exe to appear after TLauncher starts
JAVAW_WAIT_TIMEOUT = 180  # seconds
JAVAW_POLL_INTERVAL = 2   # seconds

# Windows-only creation flag; 0 on other platforms (tests run fine)
_DETACHED = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0


def find_tlauncher() -> Optional[Path]:
    """Return the first existing TLauncher path, or None."""
    for candidate in TLAUNCHER_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def launch_tlauncher(path: Optional[Path] = None) -> subprocess.Popen:
    """
    Launch TLauncher as a detached process.
    Accepts an explicit path for testing or custom installs.
    Raises FileNotFoundError if no TLauncher is found.
    """
    if path is None:
        path = find_tlauncher()
    if path is None:
        locations = "\n  ".join(str(c) for c in TLAUNCHER_CANDIDATES)
        raise FileNotFoundError(
            f"TLauncher not found. Expected one of:\n  {locations}"
        )

    if path.suffix == ".jar":
        cmd = ["java", "-jar", str(path)]
    else:
        cmd = [str(path)]

    return subprocess.Popen(cmd, creationflags=_DETACHED)


# ------------------------------------------------------------------
# Process detection
# ------------------------------------------------------------------


def find_minecraft_process(launched_after: float) -> Optional["psutil.Process"]:
    """
    Return the javaw.exe process started after `launched_after` (epoch seconds)
    whose command-line contains 'minecraft'.  Returns None if not found yet.
    """
    if psutil is None:
        raise ImportError("psutil is required for process detection; install it with pip")

    for proc in psutil.process_iter(["name", "create_time", "cmdline"]):
        try:
            name: str = proc.info["name"] or ""
            if "javaw" not in name.lower():
                continue
            if proc.info["create_time"] < launched_after:
                continue
            cmdline: list[str] = proc.info.get("cmdline") or []
            if any("minecraft" in arg.lower() for arg in cmdline):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def wait_for_minecraft(
    launched_after: float,
    timeout: float = JAVAW_WAIT_TIMEOUT,
) -> "psutil.Process":
    """
    Block until Minecraft's javaw.exe appears or timeout expires.
    Raises TimeoutError on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = find_minecraft_process(launched_after)
        if proc is not None:
            return proc
        time.sleep(JAVAW_POLL_INTERVAL)
    raise TimeoutError(
        f"Minecraft (javaw.exe) did not appear within {timeout}s. "
        "Did you open a world to LAN in-game?"
    )


def wait_for_exit(proc: "psutil.Process") -> None:
    """Block until the given process exits (or has already exited)."""
    try:
        proc.wait()
    except Exception:
        pass  # NoSuchProcess = already gone; that's fine
