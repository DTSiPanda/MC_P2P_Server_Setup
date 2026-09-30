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
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

from client.modpack_manager import get_all_world_directories

from client.admin import AdminClient, get_admin_secret, save_admin_secret
from client.api_client import APIClient, APIError, check_compatibility
from client.auth import (
    clear_api_token,
    get_api_token,
    get_saved_api_url,
    is_tailscale_logged_in,
    join_network,
    launch_tailscale_login,
    poll_join_status,
    save_api_token,
    save_api_url,
)
from client.game_launcher import find_tlauncher, save_launcher_path
from client.guest_connect import cmd_join
from client.host_session import run_host_session
from client.lan_sniffer import get_tailscale_ip, get_current_tailnet
from client.player_sync import detect_local_player_identity
from client.world_sync import clean_world_duplicates

# Default world folder in TLauncher / Minecraft saves
_APPDATA = Path(os.environ.get("APPDATA", Path.home()))
DEFAULT_WORLD_DIR = _APPDATA / ".tlauncher" / "minecraft" / "saves" / "OurWorld"
if not DEFAULT_WORLD_DIR.parent.exists():
    DEFAULT_WORLD_DIR = _APPDATA / ".minecraft" / "saves" / "OurWorld"

DEFAULT_API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000")


def ask_input_dialog(
    title: str,
    prompt: str,
    initialvalue: str = "",
    show: Optional[str] = None,
    parent: Optional[tk.Tk | tk.Toplevel] = None,
) -> Optional[str]:
    """
    Spacious prompt modal that opens in at least 800x600 resolution
    matching the application dark theme.
    """
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("800x600")
    win.minsize(800, 600)
    win.configure(background="#1e1e24")
    win.grab_set()

    result: list[Optional[str]] = [None]

    f_content = ttk.Frame(win)
    f_content.pack(fill=tk.BOTH, expand=True, padx=40, pady=40)

    ttk.Label(f_content, text=title, style="Title.TLabel").pack(anchor=tk.W, pady=(0, 16))

    ttk.Label(
        f_content,
        text=prompt,
        font=("Segoe UI", 11),
        foreground="#e0e0e0",
        wraplength=720,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 20))

    entry = ttk.Entry(f_content, font=("Segoe UI", 12))
    if show:
        entry.configure(show=show)
    entry.insert(0, initialvalue)
    entry.pack(anchor=tk.W, fill=tk.X, pady=(0, 24))
    entry.focus_set()

    def on_ok(event=None):
        result[0] = entry.get()
        win.destroy()

    def on_cancel(event=None):
        result[0] = None
        win.destroy()

    entry.bind("<Return>", on_ok)
    entry.bind("<Escape>", on_cancel)

    f_btns = ttk.Frame(f_content)
    f_btns.pack(anchor=tk.E, pady=16)

    ttk.Button(f_btns, text="Cancel", width=12, command=on_cancel).pack(side=tk.RIGHT, padx=6)
    ttk.Button(f_btns, text="OK", style="Primary.TButton", width=12, command=on_ok).pack(side=tk.RIGHT, padx=6)

    win.wait_window()
    return result[0]


def ask_confirm_dialog(
    title: str,
    prompt: str,
    parent: Optional[tk.Tk | tk.Toplevel] = None,
) -> bool:
    """
    Spacious confirmation modal (Yes/No) that opens in at least 800x600 resolution
    matching the application dark theme.
    """
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("800x600")
    win.minsize(800, 600)
    win.configure(background="#1e1e24")
    win.grab_set()

    result = [False]

    f_content = ttk.Frame(win)
    f_content.pack(fill=tk.BOTH, expand=True, padx=40, pady=40)

    ttk.Label(f_content, text=title, style="Title.TLabel").pack(anchor=tk.W, pady=(0, 16))

    ttk.Label(
        f_content,
        text=prompt,
        font=("Segoe UI", 11),
        foreground="#e0e0e0",
        wraplength=720,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 24))

    f_btns = ttk.Frame(f_content)
    f_btns.pack(anchor=tk.E, pady=16)

    def on_no():
        result[0] = False
        win.destroy()

    def on_yes():
        result[0] = True
        win.destroy()

    win.bind("<Escape>", lambda e: on_no())
    win.bind("<Return>", lambda e: on_yes())

    ttk.Button(f_btns, text="No", width=12, command=on_no).pack(side=tk.RIGHT, padx=6)
    ttk.Button(f_btns, text="Yes", style="Primary.TButton", width=12, command=on_yes).pack(side=tk.RIGHT, padx=6)

    win.wait_window()
    return result[0]


class AppUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rotating-Host Minecraft P2P v1.2.0")
        self.geometry("800x600")
        self.minsize(800, 600)


        self.api_url = get_saved_api_url()
        self.world_dir = DEFAULT_WORLD_DIR
        self.log_queue = queue.Queue()
        self.host_thread: Optional[threading.Thread] = None
        self.is_hosting = False

        # Clean up any legacy duplicate world folders (*_prev) from saves
        try:
            clean_world_duplicates(self.world_dir.parent)
        except Exception:
            pass

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
        style.configure("Badge.TLabel", font=("Segoe UI", 9, "bold"), background="#2b2d42", foreground="#4cc9f0", padding=(6, 2))
        style.configure("TLabelframe", background="#1e1e24", foreground="#a0a0b0", relief=tk.GROOVE)
        style.configure("TLabelframe.Label", background="#1e1e24", foreground="#4cc9f0", font=("Segoe UI", 9, "bold"))
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
                msg_lower = msg.lower()
                tag = "normal"
                if "error" in msg_lower or "failed" in msg_lower or "exception" in msg_lower or "⚠️" in msg:
                    tag = "err"
                elif "warn" in msg_lower or "declined" in msg_lower or "notice" in msg_lower:
                    tag = "warn"
                elif msg.startswith("[host]"):
                    tag = "host"
                elif msg.startswith("[guest]"):
                    tag = "guest"
                elif msg.startswith("[cleaner]") or msg.startswith("[tailnet]"):
                    tag = "info"
                elif "success" in msg_lower or "ready" in msg_lower or "🟢" in msg:
                    tag = "guest"

                self.log_text.insert(tk.END, msg + "\n", tag)
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

        # Server URL display & change option for custom self-hosted backends
        def on_change_server_url():
            new_url = ask_input_dialog(
                "Custom Server URL",
                "Enter your backend server API URL:\n(Leave default if using standard setup)",
                initialvalue=self.api_url,
                parent=self,
            )
            if new_url and new_url.strip():
                clean_url = new_url.strip().rstrip("/")
                save_api_url(clean_url)
                self.api_url = clean_url
                lbl_server_endpoint.configure(text=f"Server: {self.api_url}")
                messagebox.showinfo("Server Updated", f"Connected server set to:\n{self.api_url}", parent=self)

        lbl_server_endpoint = ttk.Label(
            self.frame_onboarding,
            text=f"Server: {self.api_url}",
            font=("Segoe UI", 8),
            foreground="#606070",
            cursor="hand2",
        )
        lbl_server_endpoint.pack(pady=(10, 2))
        lbl_server_endpoint.bind("<Button-1>", lambda e: on_change_server_url())

    def _show_admin_login_dialog(self):
        """Allow the host/admin to enter their Server URL, Master Token, and Admin Secret directly in the UI."""
        dlg = tk.Toplevel(self)
        dlg.title("Host / Admin Configuration & Login")
        dlg.geometry("800x600")
        dlg.minsize(800, 600)
        dlg.configure(background="#1e1e24")
        dlg.grab_set()

        f_container = ttk.Frame(dlg)
        f_container.pack(fill=tk.BOTH, expand=True, padx=40, pady=30)

        ttk.Label(f_container, text="Admin Configuration & Sign In", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(
            f_container,
            text="If hosting on your own server, enter your Render/custom API URL below.\n"
                 "Otherwise, leave default to use the standard backend.",
            justify=tk.LEFT,
            foreground="#a0a0b0",
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(0, 20))

        f_inputs = ttk.Frame(f_container)
        f_inputs.pack(fill=tk.X, pady=6)

        ttk.Label(f_inputs, text="Server API URL:", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky=tk.W, pady=10)
        entry_url = ttk.Entry(f_inputs, font=("Segoe UI", 11))
        entry_url.insert(0, self.api_url)
        entry_url.grid(row=0, column=1, sticky=tk.EW, pady=10, padx=(16, 0))

        ttk.Label(f_inputs, text="Master Token (PLAYER_TOKEN):", font=("Segoe UI", 10, "bold")).grid(row=1, column=0, sticky=tk.W, pady=10)
        entry_tok = ttk.Entry(f_inputs, show="*", font=("Segoe UI", 11))
        init_tok = get_api_token() or os.getenv("PLAYER_TOKEN") or ""
        entry_tok.insert(0, init_tok)
        entry_tok.grid(row=1, column=1, sticky=tk.EW, pady=10, padx=(16, 0))

        ttk.Label(f_inputs, text="Admin Secret (ADMIN_SECRET):", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky=tk.W, pady=10)
        entry_sec = ttk.Entry(f_inputs, show="*", font=("Segoe UI", 11))
        init_sec = get_admin_secret() or os.getenv("ADMIN_SECRET") or ""
        entry_sec.insert(0, init_sec)
        entry_sec.grid(row=2, column=1, sticky=tk.EW, pady=10, padx=(16, 0))

        f_inputs.columnconfigure(1, weight=1)

        lbl_status = ttk.Label(f_container, text="", font=("Segoe UI", 10), foreground="#4cc9f0")
        lbl_status.pack(anchor=tk.W, pady=12)

        def do_login():
            url = entry_url.get().strip().rstrip("/")
            tok = entry_tok.get().strip()
            sec = entry_sec.get().strip()

            if not url:
                messagebox.showwarning("Missing URL", "Please enter the server API URL.", parent=dlg)
                return
            if not tok:
                messagebox.showwarning("Missing Token", "Please enter your PLAYER_TOKEN.", parent=dlg)
                return

            btn_submit.configure(state=tk.DISABLED)
            lbl_status.configure(text="⏳ Connecting to server (may take 20s if waking up)...", foreground="#4cc9f0")

            def worker():
                try:
                    import requests as _req
                    r = _req.get(f"{url}/lock/status", headers={"x-api-token": tok}, timeout=45)
                    if r.status_code == 401:
                        self.after(0, lambda: (
                            lbl_status.configure(text="❌ Invalid PLAYER_TOKEN.", foreground="#ef476f"),
                            btn_submit.configure(state=tk.NORMAL)
                        ))
                        return
                    if r.status_code != 200:
                        self.after(0, lambda: (
                            lbl_status.configure(text=f"❌ Server returned status {r.status_code}.", foreground="#ef476f"),
                            btn_submit.configure(state=tk.NORMAL)
                        ))
                        return

                    save_api_url(url)
                    self.api_url = url
                    save_api_token(tok)
                    if sec:
                        save_admin_secret(sec)

                    def on_success():
                        dlg.destroy()
                        messagebox.showinfo(
                            "Success",
                            f"Connected to server:\n{url}\n\nHost/Admin credentials verified and saved!",
                            parent=self,
                        )
                        self.show_main()

                    self.after(0, on_success)
                except Exception as exc:
                    self.after(0, lambda: (
                        lbl_status.configure(text=f"❌ Connection error: {exc}", foreground="#ffb703"),
                        btn_submit.configure(state=tk.NORMAL)
                    ))

            threading.Thread(target=worker, daemon=True).start()

        f_actions = ttk.Frame(f_container)
        f_actions.pack(anchor=tk.E, pady=(20, 0))

        ttk.Button(f_actions, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=6)
        btn_submit = ttk.Button(f_actions, text="Save & Sign In as Admin", style="Primary.TButton", command=do_login)
        btn_submit.pack(side=tk.RIGHT, padx=6)



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

                # Show the Tailscale invite URL prominently — don't just silently open browser
                if invite_url:
                    import webbrowser
                    # Try opening browser automatically (may not work on all setups)
                    try:
                        webbrowser.open(invite_url)
                    except Exception:
                        pass

                    # Always show the link in a visible dialog so it can't be missed
                    def show_invite_dialog():
                        dlg = tk.Toplevel(self)
                        dlg.title("Step 2 – Join Tailscale Network")
                        dlg.geometry("800x600")
                        dlg.minsize(800, 600)
                        dlg.configure(background="#1e1e24")
                        dlg.grab_set()

                        f_container = ttk.Frame(dlg)
                        f_container.pack(fill=tk.BOTH, expand=True, padx=40, pady=40)

                        ttk.Label(
                            f_container,
                            text="📧 Check your email OR click the link below:",
                            style="Title.TLabel",
                        ).pack(anchor=tk.W, pady=(0, 16))

                        ttk.Label(
                            f_container,
                            text="A browser tab should have opened. If not, copy this invite link and open it in your browser to join the shared Tailscale network:",
                            font=("Segoe UI", 11),
                            foreground="#e0e0e0",
                            wraplength=720,
                            justify=tk.LEFT,
                        ).pack(anchor=tk.W, pady=(0, 20))

                        # Clickable / copyable URL entry
                        url_var = tk.StringVar(value=invite_url)
                        entry_url = ttk.Entry(f_container, textvariable=url_var, font=("Segoe UI", 11))
                        entry_url.pack(fill=tk.X, pady=(0, 24))
                        entry_url.configure(state="readonly")

                        f_btns = ttk.Frame(f_container)
                        f_btns.pack(anchor=tk.W, pady=8)

                        def copy_link():
                            self.clipboard_clear()
                            self.clipboard_append(invite_url)
                            btn_copy.configure(text="✅ Copied!")

                        def open_link():
                            webbrowser.open(invite_url)

                        btn_copy = ttk.Button(f_btns, text="📋 Copy Link", style="Primary.TButton", command=copy_link)
                        btn_copy.pack(side=tk.LEFT, padx=6)
                        ttk.Button(f_btns, text="🌐 Open in Browser", command=open_link).pack(side=tk.LEFT, padx=6)
                        ttk.Button(f_btns, text="Done", width=12, command=dlg.destroy).pack(side=tk.LEFT, padx=6)

                    self.after(0, show_invite_dialog)

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

        # Top Bar: Title + Player Identity + Admin Gear
        f_top = ttk.Frame(self.frame_main)
        f_top.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(f_top, text="Minecraft P2P World Hub", style="Title.TLabel").pack(side=tk.LEFT)

        try:
            player_name, _ = detect_local_player_identity()
            if player_name and player_name != "Player":
                lbl_player = ttk.Label(
                    f_top,
                    text=f"👤 {player_name}",
                    style="Badge.TLabel",
                )
                lbl_player.pack(side=tk.LEFT, padx=(12, 0))
        except Exception:
            pass

        btn_admin = ttk.Button(f_top, text="⚙ Admin", width=10, command=self.open_admin_panel)
        btn_admin.pack(side=tk.RIGHT, padx=4)

        btn_logout = ttk.Button(f_top, text="Log Out", width=10, command=self._on_logout)
        btn_logout.pack(side=tk.RIGHT, padx=4)

        # Status Bar — Tailscale row
        f_status = ttk.Frame(self.frame_main)
        f_status.pack(fill=tk.X, pady=4)

        self.lbl_ts_status = ttk.Label(f_status, text="Tailscale: Checking...", style="Header.TLabel")
        self.lbl_ts_status.pack(side=tk.LEFT)

        self.lbl_hub_status = ttk.Label(f_status, text="Status: Ready", style="Status.TLabel")
        self.lbl_hub_status.pack(side=tk.RIGHT)

        def do_ts_connect():
            self.log("[tailscale] Launching Tailscale login...")
            if not launch_tailscale_login():
                messagebox.showinfo(
                    "Tailscale Login",
                    "Please open Tailscale from your system tray (near Windows clock) and click 'Log in' or 'Connect'."
                )

        btn_ts_login = ttk.Button(f_status, text="🔌 Connect Tailscale", command=do_ts_connect)

        def copy_my_ts_ip():
            ip = get_tailscale_ip()
            if ip:
                self.clipboard_clear()
                self.clipboard_append(ip)
                self.log(f"[app] Tailscale IP copied: {ip}")
                messagebox.showinfo("Copied", f"Your Tailscale IP copied:\n{ip}\nShare this with friends if needed.")
            else:
                messagebox.showwarning("Not Connected", "Tailscale is not connected. No IP to copy.")

        btn_copy_ts_ip = ttk.Button(f_status, text="📋 Copy My TS IP", command=copy_my_ts_ip)

        def update_tailscale_display():
            try:
                ip = get_tailscale_ip()
                if ip:
                    self.lbl_ts_status.configure(text=f"Tailscale IP: {ip}", foreground="#06d6a0")
                    btn_ts_login.pack_forget()
                    btn_copy_ts_ip.pack(side=tk.LEFT, padx=4)
                else:
                    self.lbl_ts_status.configure(text="Tailscale: Not detected", foreground="#ffb703")
                    btn_ts_login.pack(side=tk.LEFT, padx=8)
                    btn_copy_ts_ip.pack_forget()
            except Exception:
                pass
            if self.frame_main.winfo_ismapped():
                self.after(3000, update_tailscale_display)

        update_tailscale_display()

        # Live Host Status Card
        card_host = ttk.LabelFrame(self.frame_main, text=" Live Game Status ")
        card_host.pack(fill=tk.X, pady=(4, 8))

        f_card_inner = ttk.Frame(card_host)
        f_card_inner.pack(fill=tk.X, padx=10, pady=8)

        self.lbl_host_banner = ttk.Label(
            f_card_inner,
            text="⏳ Checking who's hosting...",
            font=("Segoe UI", 10, "bold"),
            foreground="#a0a0b0",
        )
        self.lbl_host_banner.pack(side=tk.LEFT)

        def _update_button_emphasis(is_live_host: bool):
            if hasattr(self, "btn_join") and hasattr(self, "btn_host"):
                if is_live_host:
                    self.btn_join.configure(style="Accent.TButton", text="🚀 Join World (Host is Online!)")
                    self.btn_host.configure(style="TButton", text="🎮 Host World")
                else:
                    self.btn_join.configure(style="TButton", text="🚀 Join World")
                    self.btn_host.configure(style="Primary.TButton", text="🎮 Host World")

        def _poll_host_status():
            token = get_api_token()
            if not token:
                return
            try:
                import requests as _req
                r = _req.get(
                    f"{self.api_url}/lock/status",
                    headers={"x-api-token": token},
                    timeout=6,
                )
                if r.status_code == 200:
                    data = r.json()
                    hosting = data.get("hosting", False)
                    lock_held = data.get("lock_held", False)
                    host_name = data.get("host_name") or data.get("holder_name")
                    world_ver = data.get("current_world_version")

                    if hosting and host_name:
                        banner_text = f"🟢 ONLINE: {host_name} is hosting — Click Join to play!"
                        banner_color = "#06d6a0"
                    elif lock_held and host_name:
                        banner_text = f"🔵 STARTING: {host_name} is loading the world..."
                        banner_color = "#4cc9f0"
                    else:
                        banner_text = "🟡 READY: Nobody is currently hosting"
                        banner_color = "#ffb703"

                    ver_text = f"Cloud: {world_ver[:16]}…" if world_ver else "Cloud: No world yet"
                    self.after(0, lambda: self.lbl_host_banner.configure(text=banner_text, foreground=banner_color))
                    self.after(0, lambda: self.lbl_world_version.configure(text=ver_text))
                    self.after(0, lambda: _update_button_emphasis(hosting and bool(host_name)))
            except Exception:
                pass
            if self.frame_main.winfo_ismapped():
                self.after(30000, _poll_host_status)

        def _manual_refresh_host():
            self.lbl_host_banner.configure(text="⏳ Refreshing status...", foreground="#a0a0b0")
            threading.Thread(target=_poll_host_status, daemon=True).start()

        btn_refresh_status = ttk.Button(
            f_card_inner,
            text="🔄 Refresh",
            width=10,
            command=_manual_refresh_host,
        )
        btn_refresh_status.pack(side=tk.RIGHT, padx=(6, 0))

        self.lbl_world_version = ttk.Label(
            f_card_inner,
            text="",
            font=("Segoe UI", 8),
            foreground="#808090",
        )
        self.lbl_world_version.pack(side=tk.RIGHT, padx=4)

        # First poll after 1s, then every 30s
        self.after(1000, _poll_host_status)


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

        # Log Window — header row with export button
        f_log_header = ttk.Frame(self.frame_main)
        f_log_header.pack(fill=tk.X, pady=(8, 2))
        ttk.Label(f_log_header, text="Session Logs:", style="Header.TLabel").pack(side=tk.LEFT)

        def export_log():
            content = self.log_text.get("1.0", tk.END).strip()
            if not content:
                messagebox.showinfo("Export Log", "No log content to export.")
                return
            save_path = filedialog.asksaveasfilename(
                title="Save Session Log",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialfile=f"mc_p2p_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            )
            if save_path:
                try:
                    Path(save_path).write_text(content, encoding="utf-8")
                    messagebox.showinfo("Exported", f"Log saved to:\n{save_path}")
                except Exception as exc:
                    messagebox.showerror("Export Error", str(exc))

        def clear_log():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state=tk.DISABLED)

        ttk.Button(f_log_header, text="💾 Export", width=9, command=export_log).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(f_log_header, text="🗑 Clear", width=8, command=clear_log).pack(side=tk.RIGHT)

        self.log_text = ScrolledText(
            self.frame_main,
            height=14,
            bg="#121216",
            fg="#e0e0e0",
            insertbackground="#ffffff",
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        self.log_text.tag_configure("normal", foreground="#e0e0e0")
        self.log_text.tag_configure("host", foreground="#4cc9f0")
        self.log_text.tag_configure("guest", foreground="#06d6a0")
        self.log_text.tag_configure("warn", foreground="#ffb703")
        self.log_text.tag_configure("err", foreground="#ef476f")
        self.log_text.tag_configure("info", foreground="#a0c4ff")
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
            res = [None]
            ev = threading.Event()
            def _prompt():
                try:
                    val = ask_input_dialog(
                        "LAN Port Entry",
                        "Could not detect Minecraft LAN broadcast automatically.\n\n"
                        "Please enter the port number displayed in your Minecraft chat\n"
                        "(e.g. 'Local game hosted on port 54321'):",
                        parent=self,
                    )
                    if val and val.strip().isdigit():
                        p = int(val.strip())
                        if 1024 <= p <= 65535:
                            res[0] = p
                except Exception:
                    pass
                finally:
                    ev.set()
            self.after(0, _prompt)
            ev.wait()
            return res[0]

        def ask_tailnet_confirm(current: str, expected: str) -> bool:
            """
            Called on the background thread; schedules a dialog on the main thread
            and waits for the result.
            """
            result = [False]
            ev = threading.Event()

            def _show():
                prompt_txt = (
                    f"The app runs on a dedicated Minecraft network:\n"
                    f"  ➜  {expected}\n\n"
                    f"Your Tailscale is currently on:\n"
                    f"  ➜  {current}\n\n"
                    f"Switching is required to host or join.\n"
                    f"Your network will be restored automatically when you finish.\n\n"
                    f"⚠️  Any other Tailscale connections (e.g. work VPN) will be\n"
                    f"temporarily interrupted during the session.\n\n"
                    f"Switch networks and continue?"
                )
                answer = ask_confirm_dialog(
                    "Network Switch Required",
                    prompt_txt,
                    parent=self,
                )
                result[0] = bool(answer)
                ev.set()

            self.after(0, _show)
            ev.wait()
            return result[0]

        def worker():
            try:
                self.world_dir.mkdir(parents=True, exist_ok=True)
                run_host_session(
                    api_url=self.api_url,
                    token=token,
                    world_dir=self.world_dir,
                    log=self.log,
                    ask_port_fn=ask_port_gui,
                    confirm_tailnet_switch_fn=ask_tailnet_confirm,
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
                import requests as _req
                # Quick pre-check: is anyone hosting?
                r = _req.get(
                    f"{self.api_url}/lock/status",
                    headers={"x-api-token": token},
                    timeout=6,
                )
                if r.status_code == 200 and not r.json().get("hosting"):
                    host_name = r.json().get("holder_name")
                    if host_name:
                        self.log(f"[guest] {host_name} is setting up — waiting for LAN to open...")
                    else:
                        self.log("[guest] Nobody is hosting right now.")

                    # Ask if they want to auto-wait
                    auto_wait = threading.Event()
                    cancel = threading.Event()

                    def ask_auto_wait():
                        ans = messagebox.askyesno(
                            "Nobody Hosting Yet",
                            "Nobody is hosting a game right now.\n\n"
                            "Do you want to auto-check every 30 seconds until someone starts hosting?\n"
                            "(You can close this app to cancel.)",
                        )
                        if ans:
                            auto_wait.set()
                        else:
                            cancel.set()
                        auto_wait.set()  # unblock either way

                    self.after(0, ask_auto_wait)
                    auto_wait.wait()

                    if cancel.is_set():
                        return

                    # Keep polling until hosting starts
                    while True:
                        time.sleep(30)
                        try:
                            r2 = _req.get(
                                f"{self.api_url}/lock/status",
                                headers={"x-api-token": token},
                                timeout=6,
                            )
                            if r2.status_code == 200 and r2.json().get("hosting"):
                                hname = r2.json().get("host_name", "Someone")
                                self.log(f"[guest] {hname} started hosting! Connecting...")
                                break
                            else:
                                self.log("[guest] Still no host... checking again in 30s.")
                        except Exception:
                            pass

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
            secret = ask_input_dialog("Admin Secret", "Enter the server ADMIN_SECRET:", show="*", parent=self)
            if not secret:
                return
            save_admin_secret(secret.strip())

        token = get_api_token()
        if not token:
            token = ask_input_dialog(
                "Master Token Required",
                "Enter your server PLAYER_TOKEN (from your Render dashboard):",
                show="*",
                parent=self,
            )
            if not token:
                return
            save_api_token(token.strip())

        admin_win = tk.Toplevel(self)
        admin_win.title("Admin Control Panel")
        admin_win.geometry("800x600")
        admin_win.minsize(800, 600)
        admin_win.configure(background="#1e1e24")

        admin_client = AdminClient(self.api_url, secret, token)

        lbl = ttk.Label(admin_win, text="Admin Control Panel", style="Title.TLabel")
        lbl.pack(pady=(12, 2))

        f_target_srv = ttk.Frame(admin_win)
        f_target_srv.pack(fill=tk.X, padx=16, pady=(0, 6))

        lbl_target_srv = ttk.Label(
            f_target_srv,
            text=f"Server: {self.api_url}",
            font=("Segoe UI", 8),
            foreground="#808090",
        )
        lbl_target_srv.pack(side=tk.LEFT)

        def change_admin_server_url():
            new_url = ask_input_dialog(
                "Target Server API URL",
                "Enter custom backend server API URL:\n(e.g. https://your-app.onrender.com)",
                initialvalue=self.api_url,
                parent=admin_win,
            )
            if new_url and new_url.strip():
                clean = new_url.strip().rstrip("/")
                save_api_url(clean)
                self.api_url = clean
                lbl_target_srv.configure(text=f"Server: {self.api_url}")
                admin_client.api_url = clean
                messagebox.showinfo("Server Updated", f"Target server updated to:\n{clean}", parent=admin_win)
                refresh_lock_status()
                refresh_players()

        def change_admin_credentials():
            curr_sec = get_admin_secret() or ""
            new_sec = ask_input_dialog(
                "Admin Secret",
                "Enter ADMIN_SECRET:",
                show="*",
                initialvalue=curr_sec,
                parent=admin_win,
            )
            if new_sec and new_sec.strip():
                save_admin_secret(new_sec.strip())
                admin_client.admin_secret = new_sec.strip()

            curr_tok = get_api_token() or ""
            new_tok = ask_input_dialog(
                "Master Player Token",
                "Enter PLAYER_TOKEN (from your Render settings):",
                show="*",
                initialvalue=curr_tok,
                parent=admin_win,
            )
            if new_tok and new_tok.strip():
                save_api_token(new_tok.strip())
                admin_client.api_token = new_tok.strip()

            messagebox.showinfo("Credentials Updated", "Admin credentials updated! Refreshing...", parent=admin_win)
            refresh_lock_status()
            refresh_players()

        ttk.Button(f_target_srv, text="✏️ Server", width=9, command=change_admin_server_url).pack(side=tk.RIGHT, padx=2)
        ttk.Button(f_target_srv, text="🔑 Credentials", width=13, command=change_admin_credentials).pack(side=tk.RIGHT, padx=2)

        # -------------------------------------------------------------
        # Lock Status — always visible at the top of admin panel
        # -------------------------------------------------------------

        lf_lock = ttk.LabelFrame(admin_win, text=" 🔒 Current Lock Status ")
        lf_lock.pack(fill=tk.X, padx=16, pady=(4, 6))

        lbl_lock_status = ttk.Label(lf_lock, text="Checking...", font=("Segoe UI", 9), foreground="#a0a0b0")
        lbl_lock_status.pack(anchor=tk.W, padx=8, pady=(4, 2))

        lbl_lock_world = ttk.Label(lf_lock, text="", font=("Segoe UI", 8), foreground="#606070")
        lbl_lock_world.pack(anchor=tk.W, padx=8, pady=(0, 4))

        def refresh_lock_status():
            try:
                import requests as _req
                r = _req.get(
                    f"{self.api_url}/lock/status",
                    headers={"x-api-token": token},
                    timeout=6,
                )
                if r.status_code == 200:
                    d = r.json()
                    held = d.get("lock_held", False)
                    holder = d.get("holder_name") or "—"
                    secs = int(d.get("seconds_remaining", 0))
                    hosting = d.get("hosting", False)
                    world = d.get("current_world_version")

                    if held:
                        status_txt = f"🔐 Held by: {holder}   ({secs}s remaining)"
                        status_txt += "   🟢 Hosting" if hosting else "   🔵 Acquiring..."
                        lbl_lock_status.configure(text=status_txt, foreground="#f72585")
                    else:
                        lbl_lock_status.configure(text="✅ No lock held — server is free", foreground="#06d6a0")

                    world_txt = f"Current world: {world}" if world else "Current world: None (no uploads yet)"
                    lbl_lock_world.configure(text=world_txt)
            except Exception as exc:
                lbl_lock_status.configure(text=f"Could not fetch lock status: {exc}", foreground="#ffb703")

        ttk.Button(lf_lock, text="🔄 Refresh", command=refresh_lock_status).pack(anchor=tk.E, padx=8, pady=(0, 4))
        refresh_lock_status()

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

            if ask_confirm_dialog("Confirm Set World", f"Upload '{sel_lbl}' and make it the active cloud world for all players?", parent=admin_win):
                try:
                    from client.world_sync import cmd_upload
                    # Force-release any stale lock before uploading so admin is never blocked
                    try:
                        admin_client.force_release_lock()
                        self.log("[admin] Force-released any stale lock before upload.")
                    except Exception:
                        pass  # No lock held or already clear — that's fine
                    self.log(f"[admin] Uploading '{target_path.name}' as active cloud world...")
                    cmd_upload(self.api_url, token, target_path)
                    self.world_dir = target_path
                    self.combo_worlds.set(sel_lbl)
                    messagebox.showinfo("Success", f"World '{target_path.name}' is now the active cloud world!", parent=admin_win)
                    refresh_world_status()
                except Exception as e:
                    messagebox.showerror("Upload Error", str(e), parent=admin_win)

        ttk.Button(f_pick, text="📤 Set as Active World", style="Primary.TButton", command=do_set_world).pack(side=tk.LEFT, padx=4)

        # Lock override and cloud wipe buttons in a row
        f_lock_btns = ttk.Frame(lf_world)
        f_lock_btns.pack(fill=tk.X, padx=8, pady=(4, 2))

        def do_force_release_lock():
            if ask_confirm_dialog(
                "Force Release Lock",
                "This will forcefully release the lock, even if someone is currently hosting.\n\n"
                "Use this when a player's lock is stuck and blocking others.\n"
                "The current host session (if any) will be disconnected.",
                parent=admin_win,
            ):
                try:
                    res = admin_client.force_release_lock()
                    prev = res.get("previous_holder") or "nobody"
                    self.log(f"[admin] Force-released lock. Previous holder: {prev}")
                    messagebox.showinfo("Lock Released", f"Lock force-released.\nPrevious holder: {prev}", parent=admin_win)
                    refresh_world_status()
                except Exception as e:
                    messagebox.showerror("Error", str(e), parent=admin_win)

        def do_cloud_clear():
            if ask_confirm_dialog(
                "⚠️ Clear ALL Cloud Data",
                "This will DELETE ALL cloud world data from R2 storage and reset the lock.\n\n"
                "• All cloud world backups will be permanently deleted.\n"
                "• Any held lock will be force-released.\n"
                "• Your local saves on this PC will NOT be deleted.\n\n"
                "Are you sure you want to completely wipe the cloud?",
                parent=admin_win,
            ):
                try:
                    res = admin_client.cloud_clear()
                    prev = res.get("previous_lock_holder") or "nobody"
                    self.log(f"[admin] Cloud fully cleared. Previous lock holder: {prev}")
                    messagebox.showinfo(
                        "Cloud Cleared",
                        "All cloud world data has been deleted.\n"
                        "Upload a new world to start fresh.",
                        parent=admin_win,
                    )
                    refresh_world_status()
                except Exception as e:
                    messagebox.showerror("Clear Error", str(e), parent=admin_win)

        ttk.Button(f_lock_btns, text="🔓 Force Release Lock", style="Accent.TButton", command=do_force_release_lock).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(f_lock_btns, text="🧹 Clear Cloud Entirely", style="Danger.TButton", command=do_cloud_clear).pack(side=tk.LEFT)

        def do_reset_world():
            if ask_confirm_dialog(
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
                err_str = str(e)
                if "401" in err_str or "403" in err_str:
                    lbl_slots.configure(
                        text="⚠️ Authentication failed: Invalid ADMIN_SECRET or PLAYER_TOKEN. Click '🔑 Credentials' above.",
                        foreground="#ef476f",
                    )
                else:
                    lbl_slots.configure(text=f"Error loading players: {e}", foreground="#ffb703")


        def revoke_selected():
            sel = list_box.curselection()
            if not sel:
                messagebox.showwarning("Select Player", "Please select a player to revoke.", parent=admin_win)
                return
            p_id = player_map.get(sel[0])
            if ask_confirm_dialog("Confirm Revoke", f"Are you sure you want to revoke player {p_id}?", parent=admin_win):
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
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = AppUI()
    app.mainloop()


if __name__ == "__main__":
    main()

