"""
lan_sniffer.py – listen for Minecraft's LAN world announcement.

Minecraft broadcasts a UDP packet to 224.0.2.60:4445 every ~1.5 s:
    [MOTD]<server name>[/MOTD][AD]<port>[/AD]

We join the multicast group and parse the port from the first matching packet.
If no announcement arrives within `timeout` seconds, returns None so the
caller can fall back to manual port entry.

Interface selection: we join on INADDR_ANY by default and let the OS route
the multicast traffic via the physical adapter.  If the host machine has
multiple adapters and multicast goes out the wrong one, the caller can pass
bind_ip=<physical adapter IP> explicitly.
"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
from typing import Optional

MULTICAST_GROUP = "224.0.2.60"
MULTICAST_PORT = 4445
_PORT_RE = re.compile(r"\[AD\](\d{1,5})\[/AD\]")
import time

DEFAULT_TIMEOUT = 120  # seconds


def sniff_port(timeout: float = DEFAULT_TIMEOUT, bind_ip: str = "") -> Optional[int]:
    """
    Listen for a Minecraft LAN announcement and return the port number.
    Returns None if no announcement arrives within `timeout` seconds.

    bind_ip: IP of the physical adapter to join the multicast group on.
             Pass "" (default) to use INADDR_ANY.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(min(timeout, 2.0))
        sock.bind(("", MULTICAST_PORT))

        local_ip = socket.inet_aton(bind_ip if bind_ip else "0.0.0.0")
        mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), local_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(1024)
                message = data.decode("utf-8", errors="ignore")
                m = _PORT_RE.search(message)
                if m:
                    port = int(m.group(1))
                    if 1 <= port <= 65535:
                        return port
            except socket.timeout:
                continue
        return None
    finally:
        sock.close()


def parse_lan_announcement(message: str) -> Optional[int]:
    """
    Parse the port from a raw LAN announcement string.
    Pure function – useful for unit-testing without a real socket.
    """
    m = _PORT_RE.search(message)
    if m:
        port = int(m.group(1))
        return port if 1 <= port <= 65535 else None
    return None


import os
import shutil
import sys

def find_tailscale_cli() -> Optional[str]:
    """Find the Tailscale CLI binary path."""
    cli = shutil.which("tailscale")
    if cli:
        return cli
    candidates = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Tailscale\tailscale.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def get_tailscale_status_json() -> Optional[dict]:
    """
    Run `tailscale status --json` and return the parsed JSON, or None on failure.
    """
    try:
        cli = find_tailscale_cli() or "tailscale"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [cli, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=flags,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json as _json
            return _json.loads(result.stdout)
    except Exception:
        pass
    return None


def get_tailscale_ip(expected_tailnet: Optional[str] = None) -> Optional[str]:
    """
    Return this machine's Tailscale IPv4 address (100.x.x.x) that belongs to
    the correct shared tailnet, or None.

    Why the tailnet matters:
      A player may be a member of multiple tailnets (their own personal one +
      the host's shared tailnet). psutil and `tailscale ip -4` return whichever
      IP comes first — which is often the *personal* tailnet IP. If the host
      posts their personal tailnet IP, guests on the shared tailnet cannot reach
      it (getsockopt / connection refused errors).

    Strategy:
      1. Use `tailscale status --json` to read Self.TailscaleIPs and
         CurrentTailnet.Name. If expected_tailnet is provided and matches the
         current session's tailnet, return Self.TailscaleIPs[0] (the IPv4).
         This is always the IP on the tailnet the user is currently logged into.
      2. Fall back to psutil interface scan (fast, no CLI).
      3. Fall back to `tailscale ip -4` CLI.

    Args:
        expected_tailnet: The tailnet name/domain the app uses (e.g.
            "tejaspandeyshield@gmail.com" or "tail1b3de9.ts.net").
            Pass this so the function can validate it's using the right network.
            If None, just returns Self.TailscaleIPs[0] from status.
    """
    # 1. Best method: parse `tailscale status --json`
    status = get_tailscale_status_json()
    if status:
        self_node = status.get("Self", {})
        self_ips: list = self_node.get("TailscaleIPs", [])
        current_tailnet: dict = status.get("CurrentTailnet", {})
        tailnet_name: str = current_tailnet.get("Name", "")
        magic_dns_suffix: str = status.get("MagicDNSSuffix", "")

        # Check if this session matches the expected tailnet (if provided)
        tailnet_matches = (
            expected_tailnet is None
            or expected_tailnet in tailnet_name
            or expected_tailnet in magic_dns_suffix
            or tailnet_name in expected_tailnet
            or magic_dns_suffix in expected_tailnet
        )

        # Extract the IPv4 from Self.TailscaleIPs (first non-IPv6 entry)
        for ip in self_ips:
            if ":" not in ip and ip.startswith("100."):  # IPv4 only, skip IPv6
                if tailnet_matches:
                    return ip
                else:
                    # Tailnet mismatch — warn but still return the IP as fallback
                    # (better than returning nothing; the caller gets to decide)
                    import logging
                    logging.getLogger("minecraft_p2p").warning(
                        f"[tailscale] Current tailnet '{tailnet_name}' does not match "
                        f"expected '{expected_tailnet}'. Using IP {ip} anyway. "
                        f"Ask your host to share their tailnet invite so you're on the same network."
                    )
                    return ip

    # 2. Fast fallback: network interface addresses via psutil
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if getattr(addr.family, "name", "") == "AF_INET" or addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith("100."):
                        parts = ip.split(".")
                        if len(parts) == 4 and parts[1].isdigit() and 64 <= int(parts[1]) <= 127:
                            return ip
    except Exception:
        pass

    # 3. CLI fallback: `tailscale ip -4`
    try:
        cli = find_tailscale_cli() or "tailscale"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [cli, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        ip = result.stdout.strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass

    return None


def get_current_tailnet() -> Optional[str]:
    """
    Return the name of the currently active tailnet (e.g. 'tejaspandeyshield@gmail.com'),
    or None if Tailscale is not running / status unavailable.
    """
    status = get_tailscale_status_json()
    if not status:
        return None
    return status.get("CurrentTailnet", {}).get("Name") or None


def switch_tailnet(tailnet_name: str) -> tuple[bool, str]:
    """
    Switch the active Tailscale tailnet to tailnet_name.

    Returns (success: bool, error_message: str).
    Uses `tailscale switch <tailnet_name>` under the hood.
    The user must have previously authenticated to this tailnet
    (which they did when they accepted the user-invite).
    """
    try:
        cli = find_tailscale_cli() or "tailscale"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [cli, "switch", tailnet_name],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout).strip()
        return False, err or f"tailscale switch exited with code {result.returncode}"
    except FileNotFoundError:
        return False, "Tailscale CLI not found. Is Tailscale installed?"
    except subprocess.TimeoutExpired:
        return False, "tailscale switch timed out (15s). Is Tailscale running?"
    except Exception as exc:
        return False, str(exc)


def ensure_on_tailnet(
    expected_tailnet: str,
    confirm_fn=None,
    log=print,
) -> tuple[bool, Optional[str]]:
    """
    Ensure the active Tailscale tailnet is expected_tailnet.

    If already correct  → (True, None)  — no switch, no prompt.
    If different tailnet → asks confirm_fn, switches if agreed.
                         → (True, original_name) on success
                         → (False, None) if declined or failed.

    Args:
        expected_tailnet: Tailnet name the app requires (from /tailnet-info).
        confirm_fn:       Optional callable(current: str, expected: str) -> bool.
                          Called by the UI to show a dialog. If None, switches silently.
        log:              Logging callable.

    Returns:
        (ok: bool, original_tailnet: Optional[str])
        Store original_tailnet and pass to restore_tailnet() when done.
    """
    current = get_current_tailnet()

    if current is None:
        log("[tailnet] Could not read current tailnet. Is Tailscale running?")
        return False, None

    if current.lower().strip() == expected_tailnet.lower().strip():
        log(f"[tailnet] Already on correct tailnet: {current}")
        return True, None

    log(f"[tailnet] Currently on '{current}', app needs '{expected_tailnet}'.")

    if confirm_fn is not None:
        user_agreed = confirm_fn(current, expected_tailnet)
    else:
        user_agreed = True  # silent mode

    if not user_agreed:
        log("[tailnet] User declined tailnet switch. Aborting.")
        return False, None

    log(f"[tailnet] Switching '{current}' → '{expected_tailnet}'…")
    ok, err = switch_tailnet(expected_tailnet)
    if not ok:
        log(f"[tailnet] Switch failed: {err}")
        return False, None

    log(f"[tailnet] Switched successfully. Will restore '{current}' when done.")
    return True, current


def restore_tailnet(original_tailnet: Optional[str], log=print) -> None:
    """
    Switch back to original_tailnet after the session ends.
    Safe to call with None (no-op if user was already on the right tailnet).
    """
    if not original_tailnet:
        return
    log(f"[tailnet] Restoring your Tailscale network to '{original_tailnet}'…")
    ok, err = switch_tailnet(original_tailnet)
    if ok:
        log(f"[tailnet] Restored to '{original_tailnet}'.")
    else:
        log(f"[tailnet] Could not restore tailnet (you may need to switch manually): {err}")
