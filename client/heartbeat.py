"""
heartbeat.py – background heartbeat thread.

Sends POST /lock/heartbeat every `interval` seconds.
Calls on_lock_lost() immediately if the server returns 410 (lock lost).
Other errors (network blip) are logged and retried next interval.
"""

from __future__ import annotations

import threading
from typing import Callable

HEARTBEAT_INTERVAL = 30  # seconds


class HeartbeatThread(threading.Thread):
    """
    Daemon thread that keeps the lock alive by calling `heartbeat_fn` periodically.

    Parameters
    ----------
    heartbeat_fn:
        Callable that sends POST /lock/heartbeat and returns the response dict.
        Must raise APIError(status_code=410, ...) when the lock is lost.
    owner_token:
        The token to pass to heartbeat_fn.
    on_lock_lost:
        Called from the heartbeat thread when a 410 is received.
        Should update UI state; must not block for long.
    interval:
        Seconds between heartbeat calls (default 30).
    """

    def __init__(
        self,
        heartbeat_fn: Callable[[str], dict],
        owner_token: str,
        on_lock_lost: Callable[[], None],
        interval: float = HEARTBEAT_INTERVAL,
    ):
        super().__init__(daemon=True, name="heartbeat")
        self._heartbeat_fn = heartbeat_fn
        self._owner_token = owner_token
        self._on_lock_lost = on_lock_lost
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the thread to stop after the current sleep/heartbeat."""
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._heartbeat_fn(self._owner_token)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 410:
                    self._on_lock_lost()
                    return  # stop the thread; lock is gone
                # Transient error (network blip, cold-start): log and keep going
                print(f"  [heartbeat] Warning: {exc}")
