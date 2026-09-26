"""Kill hung CloakBrowser/Chrome trees so a mint worker never leaves zombies.

Only PIDs we launched are touched — never a user's interactive Chrome.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterable

logger = logging.getLogger("arp_harvester")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_tree(pid: int) -> bool:
    """Force-kill ``pid`` and descendants. Returns True if a kill was attempted."""
    if pid <= 0:
        return False
    logger.warning("killing browser tree pid=%s", pid)
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def reap(pids: Iterable[int]) -> int:
    killed = 0
    for pid in list(pids):
        if not pid_alive(pid):
            continue
        if kill_tree(pid):
            killed += 1
    return killed
