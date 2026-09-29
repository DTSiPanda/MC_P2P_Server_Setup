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
DEFAULT_TIMEOUT = 60  # seconds


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
        sock.settimeout(timeout)
        sock.bind(("", MULTICAST_PORT))

        local_ip = socket.inet_aton(bind_ip if bind_ip else "0.0.0.0")
        mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), local_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        while True:
            try:
                data, _ = sock.recvfrom(1024)
                message = data.decode("utf-8", errors="ignore")
                m = _PORT_RE.search(message)
                if m:
                    port = int(m.group(1))
                    if 1 <= port <= 65535:
                        return port
            except socket.timeout:
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


def get_tailscale_ip() -> Optional[str]:
    """
    Return this machine's Tailscale IPv4 address (100.x.x.x), or None.
    Runs `tailscale ip --4`; Tailscale must be installed and logged in.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip", "--4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ip = result.stdout.strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    return None
