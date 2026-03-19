from __future__ import annotations

import threading
import time

from .constants import BUILD_GATE_SECONDS

_lock: threading.Lock | None = None
_last_build_start: float = 0.0


def activate() -> None:
    """Enable the build gate. Call before starting parallel eval workers."""
    global _lock
    _lock = threading.Lock()


def deactivate() -> None:
    """Disable the build gate. Call in finally after eval workers complete."""
    global _lock
    _lock = None


def gate_build_start() -> None:
    """Acquire the build gate, ensuring a minimum gap between xcodebuild starts."""
    global _last_build_start
    if _lock is None:
        return
    with _lock:
        now = time.monotonic()
        wait = BUILD_GATE_SECONDS - (now - _last_build_start)
        if wait > 0:
            time.sleep(wait)
        _last_build_start = time.monotonic()


def make_thread_index_initializer():
    """Create a thread initializer that assigns _anvil_idx to each worker thread."""
    thread_idx = [0]
    lock = threading.Lock()

    def _init():
        with lock:
            idx = thread_idx[0]
            thread_idx[0] += 1
        threading.current_thread()._anvil_idx = idx  # type: ignore[attr-defined]

    return _init
