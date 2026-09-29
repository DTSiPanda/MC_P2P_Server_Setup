# Phase 7: Packaging, Installer, and Antivirus / SmartScreen Guide

This document outlines how the Windows desktop client is built, distributed, and installed on friends' PCs, including how to handle Windows Defender and SmartScreen warnings.

---

## 1. Building the Executable with PyInstaller

We provide `client/minecraft_p2p.spec` which compiles the client into a standalone Windows executable (`MinecraftP2P.exe`).

### Prerequisites
```powershell
pip install pyinstaller
```

### Build Command
Run from the project root:
```powershell
pyinstaller client/minecraft_p2p.spec --distpath dist --workpath build
```
This produces:
```
dist/
└── MinecraftP2P.exe
```

---

## 2. Generating the Inno Setup Windows Installer

The file `installer/setup.iss` creates a unified Windows installer (`MinecraftP2P_Installer_Setup.exe`) that:
1. Requests one standard Windows UAC administrator prompt.
2. Checks whether **Tailscale** is installed; if not, downloads and installs it silently (`/quiet /norestart`).
3. Runs `tailscale login` to launch browser sign-in automatically.
4. Checks for TLauncher / `.minecraft` saves directories.
5. Copies `MinecraftP2P.exe` to `Program Files` (or local AppData).
6. Creates Start Menu and Desktop shortcuts.

### Building with Inno Setup Compiler
1. Install [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. Right-click `installer/setup.iss` and select **Compile**, or run via command line:
   ```cmd
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
   ```
3. The final installer is generated in `dist_installer/MinecraftP2P_Installer_Setup.exe`.

---

## 3. Handling Windows SmartScreen & Antivirus False Positives

Unsigned `.exe` files created with PyInstaller and Inno Setup do not have Microsoft reputation and may trigger Windows Defender SmartScreen.

### What your friends will see on first run:
1. **Windows SmartScreen Blue Banner**:
   > *"Windows protected your PC: Microsoft Defender SmartScreen prevented an unrecognized app from starting."*
2. **What they need to do**:
   - Click **"More info"** (underlined text).
   - Click **"Run anyway"**.
3. Once run once, Windows remembers the decision and will not prompt again.

### Why this happens:
- Generic heuristic signatures: PyInstaller extracts bundled Python bytecode to a temporary directory in memory, which generic antivirus scanners flag simply because they haven't seen the specific file hash before.
- Lack of an EV Code Signing Certificate ($300+/year).

### Recommended Mitigation for Distribution:
- **Zip distribution**: Distribute `MinecraftP2P_Installer_Setup.exe` inside a `.zip` file.
- **Inno Setup**: Packaging inside Inno Setup installer drastically reduces heuristic detections compared to raw uninstaller PyInstaller standalone binaries.
- If you have an open-source code signing certificate (e.g. SignPath or Certum), sign the binary with `signtool.exe`.

---

## 4. Acceptance Test on a Clean Windows PC

1. Send `MinecraftP2P_Installer_Setup.exe` to a friend.
2. The friend runs the installer $\to$ clicks "More info" $\to$ "Run anyway".
3. Tailscale installs silently.
4. App opens with the **Onboarding Wizard**:
   - Friend enters: Single-use Invite Code (from you via Admin Panel), their Email, and Display Name.
   - The app asks Tailscale API to send them an invite and opens the link.
   - Friend signs in to Tailscale with Google/Microsoft/GitHub.
5. The app automatically detects Tailscale connectivity $\to$ unlocks the Main Dashboard!
6. Either player clicks **Host World**; the other player clicks **Join World** $\to$ `servers.dat` is automatically written and Minecraft connects over the private Tailscale mesh!
