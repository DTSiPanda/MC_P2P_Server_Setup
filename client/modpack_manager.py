"""
modpack_manager.py – Detects and manages Minecraft worlds across standard,
TLauncher isolated versions, CurseForge, PrismLauncher, Modrinth, and FTB modpacks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

_APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
_USERPROFILE = Path(os.environ.get("USERPROFILE", Path.home()))


def get_all_world_directories() -> Dict[str, Path]:
    """
    Scans the system for all Minecraft world save folders across standard installs
    and known modpack launchers.

    Returns a dict mapping formatted display label -> Path to the world folder.
    """
    worlds: Dict[str, Path] = {}

    def add_worlds_from_saves_dir(saves_dir: Path, source_label: str):
        if not saves_dir.exists() or not saves_dir.is_dir():
            return
        try:
            for item in sorted(saves_dir.iterdir()):
                if item.is_dir():
                    # Basic check for level.dat or subdirectories typical of a MC save
                    label = f"{item.name} [{source_label}]"
                    worlds[label] = item
        except Exception:
            pass

    # 1. Standard Minecraft Saves
    add_worlds_from_saves_dir(_APPDATA / ".minecraft" / "saves", "Standard")
    add_worlds_from_saves_dir(_APPDATA / ".tlauncher" / "minecraft" / "saves", "TLauncher")

    # 2. TLauncher / Vanilla Isolated Version Modpack Saves
    # e.g. %APPDATA%/.minecraft/versions/<VersionName>/saves/
    versions_dir = _APPDATA / ".minecraft" / "versions"
    if versions_dir.exists():
        try:
            for v_folder in sorted(versions_dir.iterdir()):
                if v_folder.is_dir():
                    v_saves = v_folder / "saves"
                    if v_saves.exists():
                        # Shorten version name if too long
                        v_name = v_folder.name.split("-")[0].strip()
                        add_worlds_from_saves_dir(v_saves, f"Modpack: {v_name}")
        except Exception:
            pass

    # 3. CurseForge Modpack Instances
    # e.g. %USERPROFILE%/curseforge/minecraft/Instances/<PackName>/saves/
    cf_instances = _USERPROFILE / "curseforge" / "minecraft" / "Instances"
    if cf_instances.exists():
        try:
            for p_folder in sorted(cf_instances.iterdir()):
                if p_folder.is_dir():
                    cf_saves = p_folder / "saves"
                    if cf_saves.exists():
                        add_worlds_from_saves_dir(cf_saves, f"CurseForge: {p_folder.name}")
        except Exception:
            pass

    # 4. Prism Launcher Instances
    # e.g. %APPDATA%/PrismLauncher/instances/<InstanceName>/.minecraft/saves/
    prism_dir = _APPDATA / "PrismLauncher" / "instances"
    if prism_dir.exists():
        try:
            for inst in sorted(prism_dir.iterdir()):
                if inst.is_dir():
                    p_saves = inst / ".minecraft" / "saves"
                    if not p_saves.exists():
                        p_saves = inst / "saves"
                    if p_saves.exists():
                        add_worlds_from_saves_dir(p_saves, f"Prism: {inst.name}")
        except Exception:
            pass

    # 5. Modrinth App Profiles
    # e.g. %APPDATA%/com.modrinth.theseus/profiles/<ProfileName>/saves/
    modrinth_dir = _APPDATA / "com.modrinth.theseus" / "profiles"
    if modrinth_dir.exists():
        try:
            for p_folder in sorted(modrinth_dir.iterdir()):
                if p_folder.is_dir():
                    m_saves = p_folder / "saves"
                    if m_saves.exists():
                        add_worlds_from_saves_dir(m_saves, f"Modrinth: {p_folder.name}")
        except Exception:
            pass

    # 6. FTB App Instances
    # e.g. %LOCALAPPDATA%/.ftba/instances/<id>/saves/
    ftb_dir = _LOCALAPPDATA / ".ftba" / "instances"
    if ftb_dir.exists():
        try:
            for inst in sorted(ftb_dir.iterdir()):
                if inst.is_dir():
                    ftb_saves = inst / "saves"
                    if ftb_saves.exists():
                        add_worlds_from_saves_dir(ftb_saves, f"FTB: {inst.name}")
        except Exception:
            pass

    return worlds


def get_modpack_servers_dat_candidates() -> List[Path]:
    """
    Returns a list of all detected servers.dat files across modpack instances,
    allowing guest connect to write to modpack profiles automatically.
    """
    candidates: List[Path] = [
        _APPDATA / ".minecraft" / "servers.dat",
        _APPDATA / ".tlauncher" / "minecraft" / "servers.dat",
    ]

    # Check version dirs
    versions_dir = _APPDATA / ".minecraft" / "versions"
    if versions_dir.exists():
        try:
            for v_folder in sorted(versions_dir.iterdir()):
                if v_folder.is_dir():
                    s_file = v_folder / "servers.dat"
                    if s_file.exists():
                        candidates.append(s_file)
        except Exception:
            pass

    # Check CurseForge
    cf_instances = _USERPROFILE / "curseforge" / "minecraft" / "Instances"
    if cf_instances.exists():
        try:
            for p_folder in sorted(cf_instances.iterdir()):
                if p_folder.is_dir():
                    s_file = p_folder / "servers.dat"
                    if s_file.exists():
                        candidates.append(s_file)
        except Exception:
            pass

    return candidates
