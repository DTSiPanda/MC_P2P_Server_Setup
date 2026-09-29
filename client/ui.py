"""
ui.py – Modern Desktop GUI for Rotating-Host Minecraft P2P App.

Features:
- Onboarding Wizard: asks for app invite code, email, display name,
  triggers Tailscale invite, polls status, and stores API token.
- Main Dashboard:
  - One-click 'Host World' (starts lock, download, TLauncher, sniffing, heartbeat).
  - One-click 'Join World' (writes servers.dat, opens multiplayer).
  - Status banner & live scrolling log console.
- Admin Panel (accessible via admin button or Ctrl+Shift+A):
  - Generate single-use invite codes.
  - View players & Tailscale slot usage (X/6).
  - Revoke player access.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

from client.modpack_manager import get_all_world_directories

from client.admin import AdminClient, get_admin_secret, save_admin_secret
from client.api_client import APIClient, APIError, check_compatibility
from client.auth import (
    clear_api_token,
    get_api_token,
    is_tailscale_logged_in,
    join_network,
    launch_tailscale_login,
    poll_join_status,
)
from client.game_launcher import find_tlauncher, save_launcher_path
from client.guest_connect import cmd_join
from client.host_session import run_host_session
from client.lan_sniffer import get_tailscale_ip

# Default Render API URL
DEFAULT_API_URL = os.getenv("API_URL", "https://mc-p2p-server-setup.onrender.com")

# Default world folder in TLauncher / Minecraft saves
_APPDATA = Path(os.environ.get("APPDATA", Path.home()))
DEFAULT_WORLD_DIR = _APPDATA / ".tlauncher" / "minecraft" / "saves" / "OurWorld"
if not DEFAULT_WORLD_DIR.parent.exists():
    DEFAULT_WORLD_DIR = _APPDATA / ".minecraft" / "saves" / "OurWorld"


class AppUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rotating-Host Minecraft P2P")
        self.geometry("680x560")
        self.minsize(580, 480)

        self.api_url = DEFAULT_API_URL
        self.world_dir = DEFAULT_WORLD_DIR
        self.log_queue = queue.Queue()
        self.host_thread: Optional[threading.Thread] = None
        self.is_hosting = False

        self._apply_theme()
        self._build_widgets()

        # Check credentials: show onboarding or main view
        token = get_api_token()
        if not token:
            self.show_onboarding()
        else:
            self.show_main()

        self.after(100, self._process_log_queue)
        self.bind("<Control-Shift-A>", lambda e: self.open_admin_panel())

    def _apply_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background="#1e1e24")
        style.configure("TLabel", background="#1e1e24", foreground="#ffffff", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"), foreground="#4cc9f0")
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"), foreground="#a0a0b0")
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"), foreground="#06d6a0")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("Primary.TButton", background="#4361ee", foreground="#ffffff")
        style.configure("Accent.TButton", background="#06d6a0", foreground="#000000")
        style.configure("Danger.TButton", background="#ef476f", foreground="#ffffff")

        self.configure(background="#1e1e24")

    def _build_widgets(self):
        self.container = ttk.Frame(self)
        self.container.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        # Onboarding Frame
        self.frame_onboarding = ttk.Frame(self.container)

        # Main Frame
        self.frame_main = ttk.Frame(self.container)

    def log(self, message: str):
        self.log_queue.put(message)

    def _process_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            if hasattr(self, "log_text"):
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        self.after(100, self._process_log_queue)

    # -----------------------------------------------------------------------
    # Onboarding Wizard View
    # -----------------------------------------------------------------------

    def show_onboarding(self):
        self.frame_main.pack_forget()
        self.frame_onboarding.pack(fill=tk.BOTH, expand=True)

        for w in self.frame_onboarding.winfo_children():
            w.destroy()

        lbl_title = ttk.Label(self.frame_onboarding, text="Welcome to Shared Minecraft P2P", style="Title.TLabel")
        lbl_title.pack(pady=(10, 8))

        lbl_desc = ttk.Label(
            self.frame_onboarding,
            text="To join your friends' world, enter your single-use app invite code below.\n"
                 "The app will send a Tailscale invite to your email to securely connect everyone.",
            justify=tk.CENTER,
        )
        lbl_desc.pack(pady=(0, 20))

        # Form fields
        f_fields = ttk.Frame(self.frame_onboarding)
        f_fields.pack(pady=10)

        ttk.Label(f_fields, text="Invite Code:").grid(row=0, column=0, sticky=tk.W, pady=6)
        self.entry_code = ttk.Entry(f_fields, width=28, font=("Segoe UI", 10))
        self.entry_code.grid(row=0, column=1, pady=6, padx=8)

        ttk.Label(f_fields, text="Your Email:").grid(row=1, column=0, sticky=tk.W, pady=6)
        self.entry_email = ttk.Entry(f_fields, width=28, font=("Segoe UI", 10))
        self.entry_email.grid(row=1, column=1, pady=6, padx=8)

        ttk.Label(f_fields, text="Display Name:").grid(row=2, column=0, sticky=tk.W, pady=6)
        self.entry_name = ttk.Entry(f_fields, width=28, font=("Segoe UI", 10))
        self.entry_name.grid(row=2, column=1, pady=6, padx=8)

        self.btn_join_submit = ttk.Button(
            self.frame_onboarding,
            text="Submit & Join Tailnet",
            style="Primary.TButton",
            command=self._on_submit_onboarding,
        )
        self.btn_join_submit.pack(pady=12)

        self.lbl_onboarding_status = ttk.Label(self.frame_onboarding, text="", style="Status.TLabel")
        self.lbl_onboarding_status.pack(pady=4)

        # Host / Admin Sign-in divider & button
        f_divider = ttk.Frame(self.frame_onboarding)
        f_divider.pack(fill=tk.X, pady=(20, 8))
        ttk.Separator(f_divider, orient=tk.HORIZONTAL).pack(fill=tk.X)

        btn_admin_login = ttk.Button(
            self.frame_onboarding,
            text="👑 I am the Server Host / Admin (Sign In)",
            command=self._show_admin_login_dialog,
        )
        btn_admin_login.pack(pady=6)

    def _show_admin_login_dialog(self):
        """Allow the host/admin to enter their Master Token and Admin Secret directly in the UI."""
        dlg = tk.Toplevel(self)
        dlg.title("Host / Admin Login")
        dlg.geometry("450x260")
        dlg.configure(background="#1e1e24")

        ttk.Label(dlg, text="Admin Sign In", style="Title.TLabel").pack(pady=(12, 6))
        ttk.Label(
            dlg,
            text="Enter the Master Player Token and Admin Secret configured on Render.",
            justify=tk.CENTER,
        ).pack(pady=(0, 12))

        f_inputs = ttk.Frame(dlg)
        f_inputs.pack(pady=6)

        ttk.Label(f_inputs, text="Master Token (PLAYER_TOKEN):").grid(row=0, column=0, sticky=tk.W, pady=6)
        entry_tok = ttk.Entry(f_inputs, width=28, show="*", font=("Segoe UI", 10))
        entry_tok.grid(row=0, column=1, pady=6, padx=8)

        ttk.Label(f_inputs, text="Admin Secret (ADMIN_SECRET):").grid(row=1, column=0, sticky=tk.W, pady=6)
        entry_sec = ttk.Entry(f_inputs, width=28, show="*", font=("Segoe UI", 10))
        entry_sec.grid(row=1, column=1, pady=6, padx=8)

        def do_login():
            tok = entry_tok.get().strip()
            sec = entry_sec.get().strip()
            if not tok:
                messagebox.showwarning("Missing Token", "Please enter your PLAYER_TOKEN.", parent=dlg)
                return

            from client.auth import save_api_token
            save_api_token(tok)
            if sec:
                save_admin_secret(sec)

            dlg.destroy()
            messagebox.showinfo("Success", "Host/Admin credentials saved! Loading main dashboard.", parent=self)
            self.show_main()

        ttk.Button(dlg, text="Sign In as Host", style="Primary.TButton", command=do_login).pack(pady=16)

    def _on_submit_onboarding(self):
        code = self.entry_code.get().strip()
        email = self.entry_email.get().strip()
        name = self.entry_name.get().strip()

        if not code or not email or not name:
            messagebox.showwarning("Incomplete", "Please fill in all three fields.")
            return

        self.btn_join_submit.configure(state=tk.DISABLED)
        self.lbl_onboarding_status.configure(text="Contacting API and creating Tailscale invite...")

        def worker():
            try:
                res = join_network(self.api_url, code, email, name)
                invite_url = res.get("tailscale_invite_url")
                token = res["api_token"]

                self.lbl_onboarding_status.configure(
                    text="Invite created! Checking Tailscale acceptance..."
                )

                # Prompt user with invite URL if available
                if invite_url:
                    import webbrowser
                    webbrowser.open(invite_url)

                # Poll status in background
                accepted = poll_join_status(self.api_url, token, timeout=120, poll_interval=3, log=self.log)
                if accepted:
                    self.after(0, lambda: messagebox.showinfo("Success", "Onboarding complete! Welcome to the group."))
                    self.after(0, self.show_main)
                else:
                    self.after(0, lambda: messagebox.showwarning("Notice", "Token saved! Please accept your email invite and restart the app."))
                    self.after(0, self.show_main)

            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Join Failed", str(exc)))
                self.after(0, lambda: self.btn_join_submit.configure(state=tk.NORMAL))
                self.after(0, lambda: self.lbl_onboarding_status.configure(text=""))

        threading.Thread(target=worker, daemon=True).start()

    # -----------------------------------------------------------------------
    # Main Dashboard View
    # -----------------------------------------------------------------------

    def show_main(self):
        self.frame_onboarding.pack_forget()
        self.frame_main.pack(fill=tk.BOTH, expand=True)

        for w in self.frame_main.winfo_children():
            w.destroy()

        # Top Bar: Title + Admin Gear
        f_top = ttk.Frame(self.frame_main)
        f_top.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(f_top, text="Minecraft P2P World Hub", style="Title.TLabel").pack(side=tk.LEFT)

        btn_admin = ttk.Button(f_top, text="⚙ Admin", width=10, command=self.open_admin_panel)
        btn_admin.pack(side=tk.RIGHT, padx=4)

        btn_logout = ttk.Button(f_top, text="Log Out", width=10, command=self._on_logout)
        btn_logout.pack(side=tk.RIGHT, padx=4)

        # Status Bar
        f_status = ttk.Frame(self.frame_main)
        f_status.pack(fill=tk.X, pady=4)

        self.lbl_ts_status = ttk.Label(f_status, text="Tailscale: Checking...", style="Header.TLabel")
        self.lbl_ts_status.pack(side=tk.LEFT)

        self.lbl_hub_status = ttk.Label(f_status, text="Status: Ready", style="Status.TLabel")
        self.lbl_hub_status.pack(side=tk.RIGHT)

        def update_tailscale_display():
            try:
                ip = get_tailscale_ip()
                if ip:
                    self.lbl_ts_status.configure(text=f"Tailscale IP: {ip}", foreground="#06d6a0")
                else:
                    self.lbl_ts_status.configure(text="Tailscale: Not detected", foreground="#ffb703")
            except Exception:
                pass
            if self.frame_main.winfo_ismapped():
                self.after(4000, update_tailscale_display)

        update_tailscale_display()

        # World Selection Row
        f_world = ttk.Frame(self.frame_main)
        f_world.pack(fill=tk.X, pady=(6, 8))

        ttk.Label(f_world, text="World to Sync:").pack(side=tk.LEFT, padx=(0, 6))

        self.world_map = get_all_world_directories()
        display_labels = list(self.world_map.keys()) if self.world_map else ["OurWorld [Standard]"]

        # Default selection priority: OurWorld if exists, else first
        default_label = display_labels[0]
        for lbl in display_labels:
            if "OurWorld" in lbl:
                default_label = lbl
                break

        self.world_dir = self.world_map.get(default_label, DEFAULT_WORLD_DIR)

        self.combo_worlds = ttk.Combobox(f_world, values=display_labels, font=("Segoe UI", 9), width=28)
        self.combo_worlds.set(default_label)
        self.combo_worlds.pack(side=tk.LEFT, padx=4)

        def on_world_change(event=None):
            sel = self.combo_worlds.get().strip()
            if sel in self.world_map:
                self.world_dir = self.world_map[sel]
                self.log(f"[app] Selected world: {self.world_dir}")

        self.combo_worlds.bind("<<ComboboxSelected>>", on_world_change)

        def on_browse_world():
            chosen = filedialog.askdirectory(title="Select Minecraft World Folder (containing level.dat)")
            if chosen:
                p = Path(chosen)
                lbl = f"{p.name} [Custom: {p.parent.name}]"
                self.world_map[lbl] = p
                self.combo_worlds["values"] = list(self.world_map.keys())
                self.combo_worlds.set(lbl)
                self.world_dir = p
                self.log(f"[app] Custom world chosen: {self.world_dir}")

        btn_browse = ttk.Button(f_world, text="📁 Browse...", width=10, command=on_browse_world)
        btn_browse.pack(side=tk.LEFT, padx=4)

        btn_manual_upload = ttk.Button(f_world, text="☁ Upload to Cloud", command=self._on_click_manual_upload)
        btn_manual_upload.pack(side=tk.LEFT, padx=4)

        # Launcher Row
        f_launcher = ttk.Frame(self.frame_main)
        f_launcher.pack(fill=tk.X, pady=(2, 6))

        detected_launcher = find_tlauncher()
        launcher_display = detected_launcher.name if detected_launcher else "Not detected"
        self.lbl_launcher = ttk.Label(f_launcher, text=f"Launcher: {launcher_display}", font=("Segoe UI", 9))
        self.lbl_launcher.pack(side=tk.LEFT, padx=2)

        def on_browse_launcher():
            chosen = filedialog.askopenfilename(
                title="Select Minecraft Launcher (e.g. TLauncher.exe)",
                filetypes=[("Executables", "*.exe;*.jar;*.lnk"), ("All files", "*.*")],
                parent=self,
            )
            if chosen:
                save_launcher_path(chosen)
                self.lbl_launcher.configure(text=f"Launcher: {Path(chosen).name}")
                self.log(f"[app] Launcher path set to: {chosen}")

        btn_browse_launcher = ttk.Button(f_launcher, text="Select Launcher...", width=16, command=on_browse_launcher)
        btn_browse_launcher.pack(side=tk.RIGHT, padx=4)

        # Action Buttons
        f_actions = ttk.Frame(self.frame_main)
        f_actions.pack(fill=tk.X, pady=8)

        self.btn_host = ttk.Button(
            f_actions,
            text="🎮 Host World",
            style="Primary.TButton",
            command=self._on_click_host,
        )
        self.btn_host.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.btn_join = ttk.Button(
            f_actions,
            text="🚀 Join World",
            style="Accent.TButton",
            command=self._on_click_join,
        )
        self.btn_join.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.btn_copy_addr = ttk.Button(
            f_actions,
            text="📋 Copy Address",
            command=self._on_click_copy_address,
        )
        self.btn_copy_addr.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # Log Window
        ttk.Label(self.frame_main, text="Session Logs:", style="Header.TLabel").pack(anchor=tk.W, pady=(8, 2))

        self.log_text = ScrolledText(
            self.frame_main,
            height=14,
            bg="#121216",
            fg="#e0e0e0",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.log(f"[app] Connected to API: {self.api_url}")
        self.log(f"[app] World save folder: {self.world_dir}")

    def _on_logout(self):
        if messagebox.askyesno("Log Out", "Log out and return to the welcome screen?"):
            clear_api_token()
            self.show_onboarding()

    def _on_click_manual_upload(self):
        token = get_api_token()
        if not token:
            messagebox.showerror("Error", "No API token found.")
            return

        if not self.world_dir.exists():
            messagebox.showerror("Missing Folder", f"World folder does not exist:\n{self.world_dir}")
            return

        self.lbl_hub_status.configure(text="Status: Uploading to Cloud...")

        def worker():
            try:
                from client.world_sync import cmd_upload
                self.log(f"[upload] Uploading '{self.world_dir.name}' to cloud...")
                cmd_upload(self.api_url, token, self.world_dir)
                self.log(f"[upload] Successfully uploaded '{self.world_dir.name}' to Cloudflare R2!")
                self.after(0, lambda: messagebox.showinfo("Success", f"World '{self.world_dir.name}' is now live in the cloud!"))
            except Exception as exc:
                self.log(f"[upload] Error: {exc}")
                self.after(0, lambda: messagebox.showerror("Upload Error", str(exc)))
            finally:
                self.after(0, lambda: self.lbl_hub_status.configure(text="Status: Ready", foreground="#06d6a0"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_click_host(self):
        token = get_api_token()
        if not token:
            messagebox.showerror("Error", "No API token found. Please rejoin.")
            return

        if self.is_hosting:
            messagebox.showinfo("Hosting", "A hosting session is already running.")
            return

        self.is_hosting = True
        self.btn_host.configure(state=tk.DISABLED)
        self.lbl_hub_status.configure(text="Status: Hosting...", foreground="#4cc9f0")

        def ask_port_gui() -> Optional[int]:
            import tkinter.simpledialog as sd
            res = [None]
            ev = threading.Event()
            def _prompt():
                try:
                    p = sd.askinteger(
                        "LAN Port Entry",
                        "Could not detect Minecraft LAN broadcast automatically.\n\n"
                        "Please enter the port number displayed in your Minecraft chat\n"
                        "(e.g. 'Local game hosted on port 54321'):",
                        parent=self,
                        minvalue=1024,
                        maxvalue=65535,
                    )
                    res[0] = p
                except Exception:
                    pass
                finally:
                    ev.set()
            self.after(0, _prompt)
            ev.wait()
            return res[0]

        def worker():
            try:
                self.world_dir.mkdir(parents=True, exist_ok=True)
                run_host_session(
                    api_url=self.api_url,
                    token=token,
                    world_dir=self.world_dir,
                    log=self.log,
                    ask_port_fn=ask_port_gui,
                )
            except (Exception, SystemExit) as exc:
                self.log(f"[host] Session ended: {exc}")
            except BaseException as exc:
                self.log(f"[host] Unexpected session termination: {exc}")
            finally:
                self.is_hosting = False
                self.after(0, lambda: self.btn_host.configure(state=tk.NORMAL))
                self.after(0, lambda: self.lbl_hub_status.configure(text="Status: Ready", foreground="#06d6a0"))

        self.host_thread = threading.Thread(target=worker, daemon=True)
        self.host_thread.start()

    def _on_click_join(self):
        token = get_api_token()
        if not token:
            messagebox.showerror("Error", "No API token found.")
            return

        self.btn_join.configure(state=tk.DISABLED)
        self.lbl_hub_status.configure(text="Status: Connecting as Guest...")

        def worker():
            try:
                cmd_join(self.api_url, token, log=self.log)
            except Exception as exc:
                self.log(f"[guest] Error: {exc}")
            finally:
                self.after(0, lambda: self.btn_join.configure(state=tk.NORMAL))
                self.after(0, lambda: self.lbl_hub_status.configure(text="Status: Ready", foreground="#06d6a0"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_click_copy_address(self):
        token = get_api_token()
        if not token:
            return

        def worker():
            try:
                client = APIClient(self.api_url, token)
                host_info = client.get_host()
                if host_info.get("hosting"):
                    addr = f"{host_info['ip']}:{host_info['port']}"
                    self.clipboard_clear()
                    self.clipboard_append(addr)
                    self.log(f"[app] Copied host address to clipboard: {addr}")
                    self.after(0, lambda: messagebox.showinfo("Copied", f"Host address copied:\n{addr}"))
                else:
                    self.log("[app] Nobody is currently hosting.")
                    self.after(0, lambda: messagebox.showinfo("Host Info", "Nobody is currently hosting."))
            except Exception as exc:
                self.log(f"[app] Could not fetch host: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    # -----------------------------------------------------------------------
    # Admin Panel Modal
    # -----------------------------------------------------------------------

    def open_admin_panel(self):
        secret = get_admin_secret()
        if not secret:
            secret = simpledialog.askstring("Admin Secret", "Enter the server ADMIN_SECRET:", show="*")
            if not secret:
                return
            save_admin_secret(secret)

        token = get_api_token()
        if not token:
            messagebox.showerror("Error", "Player API token is required.")
            return

        admin_win = tk.Toplevel(self)
        admin_win.title("Admin Control Panel")
        admin_win.geometry("620x620")
        admin_win.configure(background="#1e1e24")

        admin_client = AdminClient(self.api_url, secret, token)

        lbl = ttk.Label(admin_win, text="Admin Control Panel", style="Title.TLabel")
        lbl.pack(pady=(12, 4))

        # -------------------------------------------------------------
        # Section 1: Cloud World Management
        # -------------------------------------------------------------
        lf_world = ttk.LabelFrame(admin_win, text=" Cloud World Management ")
        lf_world.pack(fill=tk.X, padx=16, pady=8)

        lbl_curr_world = ttk.Label(lf_world, text="Current Cloud World: Checking...", font=("Segoe UI", 9))
        lbl_curr_world.pack(anchor=tk.W, padx=8, pady=(6, 2))

        # Dropdown to choose which local world to set/upload
        f_pick = ttk.Frame(lf_world)
        f_pick.pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(f_pick, text="Select Local World:").pack(side=tk.LEFT, padx=(0, 6))

        admin_world_map = get_all_world_directories()
        admin_labels = list(admin_world_map.keys()) if admin_world_map else ["OurWorld"]
        combo_admin_world = ttk.Combobox(f_pick, values=admin_labels, width=28, font=("Segoe UI", 9))
        combo_admin_world.set(self.combo_worlds.get() if self.combo_worlds.get() in admin_labels else admin_labels[0])
        combo_admin_world.pack(side=tk.LEFT, padx=4)

        def on_admin_browse():
            chosen = filedialog.askdirectory(title="Select Minecraft World Folder (containing level.dat)", parent=admin_win)
            if chosen:
                p = Path(chosen)
                lbl = f"{p.name} [Custom: {p.parent.name}]"
                admin_world_map[lbl] = p
                combo_admin_world["values"] = list(admin_world_map.keys())
                combo_admin_world.set(lbl)

        ttk.Button(f_pick, text="📁 Browse...", width=9, command=on_admin_browse).pack(side=tk.LEFT, padx=2)

        def do_set_world():
            sel_lbl = combo_admin_world.get().strip()
            target_path = admin_world_map.get(sel_lbl)
            if not target_path or not target_path.exists():
                messagebox.showerror("Error", f"World folder does not exist:\n{target_path}", parent=admin_win)
                return

            if messagebox.askyesno("Confirm Set World", f"Upload '{sel_lbl}' and make it the active cloud world for all players?", parent=admin_win):
                try:
                    from client.world_sync import cmd_upload
                    self.log(f"[admin] Uploading '{target_path.name}' as active cloud world...")
                    cmd_upload(self.api_url, token, target_path)
                    self.world_dir = target_path
                    self.combo_worlds.set(sel_lbl)
                    messagebox.showinfo("Success", f"World '{target_path.name}' is now the active cloud world!", parent=admin_win)
                    refresh_world_status()
                except Exception as e:
                    messagebox.showerror("Upload Error", str(e), parent=admin_win)

        ttk.Button(f_pick, text="📤 Set as Active World", style="Primary.TButton", command=do_set_world).pack(side=tk.LEFT, padx=4)

        def do_reset_world():
            if messagebox.askyesno(
                "Confirm Delete / Reset",
                "Are you sure you want to DELETE the cloud world?\n\n"
                "This will wipe all cloud backups and reset the server to 'fresh world' state.\n"
                "(Your local saves on your PC will NOT be deleted).",
                parent=admin_win,
            ):
                try:
                    admin_client.reset_world()
                    self.log("[admin] Cloud world wiped and reset by admin.")
                    messagebox.showinfo("Reset Complete", "The cloud world has been wiped and reset.", parent=admin_win)
                    refresh_world_status()
                except Exception as e:
                    messagebox.showerror("Reset Error", str(e), parent=admin_win)

        ttk.Button(lf_world, text="🗑️ Delete / Reset Cloud World", style="Danger.TButton", command=do_reset_world).pack(anchor=tk.E, padx=8, pady=(4, 8))

        def refresh_world_status():
            try:
                client = APIClient(self.api_url, token)
                cfg = client.get_config()
                cw = cfg.get("current_world_version")
                if cw:
                    lbl_curr_world.configure(text=f"Current Cloud World: {cw}", foreground="#4cc9f0")
                else:
                    lbl_curr_world.configure(text="Current Cloud World: None (Fresh setup)", foreground="#f72585")
            except Exception as e:
                lbl_curr_world.configure(text=f"Error checking world: {e}")

        # -------------------------------------------------------------
        # Section 2: Player & Access Management
        # -------------------------------------------------------------
        lf_players = ttk.LabelFrame(admin_win, text=" Access Control & Invites ")
        lf_players.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        # Slot summary
        lbl_slots = ttk.Label(lf_players, text="Loading slots...", style="Header.TLabel")
        lbl_slots.pack(anchor=tk.W, padx=8, pady=(4, 2))

        def make_invite():
            try:
                res = admin_client.create_invite_code(ttl_hours=48, uses=1)
                code = res["code"]
                self.clipboard_clear()
                self.clipboard_append(code)
                messagebox.showinfo(
                    "Invite Created",
                    f"New Invite Code Generated:\n\n{code}\n\n(Copied to your clipboard!)",
                    parent=admin_win,
                )
                refresh_players()
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=admin_win)

        ttk.Button(lf_players, text="➕ Generate New Single-Use Invite Code", style="Primary.TButton", command=make_invite).pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(lf_players, text="Registered Players:").pack(anchor=tk.W, padx=8, pady=(6, 2))

        list_box = tk.Listbox(lf_players, bg="#121216", fg="#ffffff", selectbackground="#4361ee", font=("Segoe UI", 9), height=7)
        list_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        player_map = {}

        def refresh_players():
            list_box.delete(0, tk.END)
            player_map.clear()
            try:
                data = admin_client.list_players()
                lbl_slots.configure(text=f"Tailscale Slots Used: {data['slots_used']} / {data['slots_max']}")
                for idx, p in enumerate(data.get("players", [])):
                    status_tag = "[REVOKED]" if p.get("revoked") else "[ACTIVE]"
                    line = f"{status_tag} {p['name']} ({p.get('email', 'no email')})  ID: {p['id']}"
                    list_box.insert(tk.END, line)
                    player_map[idx] = p["id"]
            except Exception as e:
                lbl_slots.configure(text=f"Error loading players: {e}")

        def revoke_selected():
            sel = list_box.curselection()
            if not sel:
                messagebox.showwarning("Select Player", "Please select a player to revoke.", parent=admin_win)
                return
            p_id = player_map.get(sel[0])
            if messagebox.askyesno("Confirm Revoke", f"Are you sure you want to revoke player {p_id}?", parent=admin_win):
                try:
                    admin_client.revoke_player(p_id)
                    messagebox.showinfo("Success", f"Player {p_id} has been revoked.", parent=admin_win)
                    refresh_players()
                except Exception as e:
                    messagebox.showerror("Error", str(e), parent=admin_win)

        f_bottom = ttk.Frame(lf_players)
        f_bottom.pack(fill=tk.X, padx=8, pady=(0, 6))

        ttk.Button(f_bottom, text="🔄 Refresh", command=lambda: (refresh_players(), refresh_world_status())).pack(side=tk.LEFT, padx=4)
        ttk.Button(f_bottom, text="🚫 Revoke Selected Player", style="Danger.TButton", command=revoke_selected).pack(side=tk.RIGHT, padx=4)

        # Initial data load
        refresh_world_status()
        refresh_players()


def main():
    app = AppUI()
    app.mainloop()


if __name__ == "__main__":
    main()
