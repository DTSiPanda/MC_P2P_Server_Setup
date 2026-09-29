"""
main.py – Client application main entry point.

Runs the Tkinter Desktop GUI by default, or runs CLI commands if flags are passed.
"""

from __future__ import annotations

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Minecraft Rotating Host P2P")
    parser.add_argument("--cli", action="store_true", help="Run without graphical interface")
    parser.add_argument("--host", action="store_true", help="Directly trigger host flow")
    parser.add_argument("--join", action="store_true", help="Directly trigger guest join flow")

    args, remaining = parser.parse_known_args()

    if args.host:
        from client.host_session import run_host_session
        from client.auth import get_api_token
        from client.ui import DEFAULT_API_URL, DEFAULT_WORLD_DIR
        token = get_api_token()
        if not token:
            print("ERROR: No player token found. Run GUI first to onboard.")
            sys.exit(1)
        run_host_session(DEFAULT_API_URL, token, DEFAULT_WORLD_DIR)
        return

    if args.join:
        from client.guest_connect import cmd_join
        from client.auth import get_api_token
        from client.ui import DEFAULT_API_URL
        token = get_api_token()
        if not token:
            print("ERROR: No player token found. Run GUI first to onboard.")
            sys.exit(1)
        cmd_join(DEFAULT_API_URL, token)
        return

    # Default: launch GUI
    from client.ui import main as ui_main
    ui_main()


if __name__ == "__main__":
    main()
