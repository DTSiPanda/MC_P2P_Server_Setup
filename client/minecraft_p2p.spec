# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Minecraft Rotating-Host P2P Client

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'keyring',
        'keyring.backends',
        'keyring.backends.Windows',
        'nbtlib',
        'psutil',
        'requests',
        'client.ui',
        'client.auth',
        'client.admin',
        'client.api_client',
        'client.autosave',
        'client.conflict_manager',
        'client.game_launcher',
        'client.guest_connect',
        'client.heartbeat',
        'client.host_session',
        'client.lan_sniffer',
        'client.modpack_manager',
        'client.world_sync',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'moto', 'boto3', 'botocore', 'server'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MinecraftP2P',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Windowed GUI application (no black terminal box)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
