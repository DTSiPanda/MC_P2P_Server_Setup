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

_APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
_PROGRAMFILES = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
_PROGRAMFILES_X86 = Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"))

TLAUNCHER_CANDIDATES: list[Path] = [
    # Standard TLauncher default paths (including default .minecraft/TLauncher.exe)
    _APPDATA / ".minecraft" / "TLauncher.exe",
    _APPDATA / "TLauncher" / "TLauncher.exe",
    _APPDATA / "TLauncher" / "TLauncher.jar",
    _APPDATA / ".tlauncher" / "TLauncher.exe",
    _APPDATA / ".tlauncher" / "TLauncher.jar",
    _LOCALAPPDATA / "Programs" / "TLauncher" / "TLauncher.exe",
    _LOCALAPPDATA / "TLauncher" / "TLauncher.exe",
    _PROGRAMFILES / "TLauncher" / "TLauncher.exe",
    _PROGRAMFILES_X86 / "TLauncher" / "TLauncher.exe",
    Path.home() / "TLauncher" / "TLauncher.exe",
    Path.home() / "AppData" / "Roaming" / ".minecraft" / "TLauncher.exe",
    Path.home() / "Desktop" / "TLauncher.exe",
    Path("C:/ProgramData/Microsoft/Windows/Start Menu/Programs/TLauncher/TLauncher.lnk"),
    # Common alternate Minecraft launchers
    _PROGRAMFILES_X86 / "Minecraft Launcher" / "MinecraftLauncher.exe",
    _PROGRAMFILES / "Minecraft Launcher" / "MinecraftLauncher.exe",
    _LOCALAPPDATA / "Programs" / "Minecraft Launcher" / "MinecraftLauncher.exe",
    _APPDATA / ".minecraft" / "MinecraftLauncher.exe",
    _APPDATA / ".minecraft" / "launcher.exe",
    _PROGRAMFILES / "PrismLauncher" / "prismlauncher.exe",
    _APPDATA / "PrismLauncher" / "prismlauncher.exe",
    _LOCALAPPDATA / "Programs" / "Modrinth App" / "Modrinth App.exe",
    _LOCALAPPDATA / "Programs" / "CurseForge" / "CurseForge.exe",
]

# How long to wait for javaw.exe to appear after TLauncher starts
JAVAW_WAIT_TIMEOUT = 180  # seconds
JAVAW_POLL_INTERVAL = 2   # seconds

# Windows-only creation flag; 0 on other platforms (tests run fine)
_DETACHED = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0


def get_saved_launcher_path() -> Optional[Path]:
    """Check if the user saved a custom launcher path in settings."""
    try:
        import keyring
        val = keyring.get_password("MinecraftP2P", "launcher_path")
        if val and Path(val).exists():
            return Path(val)
    except Exception:
        pass
    p = Path.home() / ".minecraft_p2p" / "launcher_path"
    if p.exists():
        try:
            val = p.read_text(encoding="utf-8").strip()
            if val and Path(val).exists():
                return Path(val)
        except Exception:
            pass
    return None


def save_launcher_path(path: Path | str) -> None:
    """Save custom launcher path."""
    p_str = str(path)
    try:
        import keyring
        keyring.set_password("MinecraftP2P", "launcher_path", p_str)
        return
    except Exception:
        pass
    d = Path.home() / ".minecraft_p2p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "launcher_path").write_text(p_str, encoding="utf-8")


def is_tlauncher_running() -> bool:
    """Return True if TLauncher is already running."""
    if psutil is None:
        return False
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(str(c).lower() for c in (proc.info.get("cmdline") or []))
            if "tlauncher" in name or "org.tlauncher" in cmdline:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def find_tlauncher() -> Optional[Path]:
    """Return the first existing TLauncher / Minecraft launcher path, or None."""
    saved = get_saved_launcher_path()
    if saved and saved.exists():
        return saved

    for candidate in TLAUNCHER_CANDIDATES:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def launch_tlauncher(path: Optional[Path] = None) -> Optional[subprocess.Popen]:
    """
    Launch TLauncher as a detached process.
    Accepts an explicit path for testing or custom installs.
    Raises FileNotFoundError if no TLauncher is found.
    """
    if path is None:
        path = find_tlauncher()
    if path is None:
        locations = "\n  ".join(str(c) for c in TLAUNCHER_CANDIDATES[:5])
        raise FileNotFoundError(
            f"TLauncher not found. Expected one of:\n  {locations}"
        )

    # If TLauncher is already open, skip launching a duplicate
    if is_tlauncher_running():
        return None

    cwd = str(path.parent) if path.parent.exists() else None

    if path.suffix.lower() == ".jar":
        cmd = ["java", "-jar", str(path)]
    elif path.suffix.lower() == ".lnk":
        cmd = ["cmd", "/c", "start", "", str(path)]
    else:
        cmd = [str(path)]

    return subprocess.Popen(cmd, cwd=cwd, creationflags=_DETACHED)


# ------------------------------------------------------------------
# Process detection
# ------------------------------------------------------------------


def find_minecraft_process(launched_after: float = 0.0) -> Optional["psutil.Process"]:
    """
    Return the javaw.exe or java.exe process started after `launched_after` (epoch seconds)
    whose command-line contains Minecraft or modpack indicators. Returns None if not found yet.
    """
    if psutil is None:
        raise ImportError("psutil is required for process detection; install it with pip")

    for proc in psutil.process_iter(["name", "create_time", "cmdline"]):
        try:
            name: str = proc.info["name"] or ""
            # On Windows Minecraft runs under javaw.exe or java.exe
            if "java" not in name.lower():
                continue
            if proc.info["create_time"] < launched_after:
                continue
            cmdline: list[str] = proc.info.get("cmdline") or []
            cmdline_str = " ".join(str(arg).lower() for arg in cmdline)
            # Skip TLauncher's own GUI process
            if "org.tlauncher.tlauncher" in cmdline_str:
                continue
            # Match Minecraft game client
            if any(k in cmdline_str for k in ("minecraft", "net.minecraft", "fabric", "forge", "fml", "optifine")):
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
        # Also check if Minecraft was already running before launch_ts
        existing = find_minecraft_process(0.0)
        if existing is not None:
            return existing
        time.sleep(JAVAW_POLL_INTERVAL)
    raise TimeoutError(
        f"Minecraft did not appear within {timeout}s. "
        "Did you launch the game and open a world to LAN?"
    )


def wait_for_exit(proc: "psutil.Process") -> None:
    """Block until the given process exits (or has already exited)."""
    try:
        proc.wait()
    except Exception:
        pass  # NoSuchProcess = already gone; that's fine
