"""
autosave.py – Background autosave thread for the hosting player (plan.md Section 6 & 7).

Periodically packs and commits the world to R2 without releasing the lock.
If the host's app or PC crashes, the last autosave snapshot is safe in cloud storage.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

AUTOSAVE_DEFAULT_INTERVAL = 600  # 10 minutes


class AutosaveThread(threading.Thread):
    def __init__(
        self,
        autosave_fn: Callable[[], None],
        interval: float = AUTOSAVE_DEFAULT_INTERVAL,
        log: Callable[[str], None] = print,
    ):
        super().__init__(daemon=True, name="autosave")
        self._autosave_fn = autosave_fn
        self._interval = interval
        self._log = log
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._log("[autosave] Starting periodic world autosave...")
                self._autosave_fn()
                self._log("[autosave] Autosave completed successfully.")
            except Exception as exc:
                self._log(f"[autosave] Warning: autosave failed: {exc}")
