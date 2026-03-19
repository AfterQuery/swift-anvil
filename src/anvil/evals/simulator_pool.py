from __future__ import annotations

import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from .constants import DEFAULT_DEVICE_NAME, SIMULATOR_NAME_PREFIX

logger = logging.getLogger(__name__)


class SimulatorPool:
    """Manages a pool of iOS Simulators for parallel test execution."""

    def __init__(self, test_destination: str):
        """xcodebuild destination string (e.g. platform=iOS Simulator,name=iPhone 16)."""
        self._test_destination = test_destination
        self._udids: list[str] = []

    def create(self, n: int) -> list[str]:
        """Create n simulators, boot them, return UDIDs."""
        device_name = self._parse_device_name(self._test_destination)
        created = 0
        created_lock = threading.Lock()

        def _create_one(i: int) -> str:
            nonlocal created
            sim_name = f"{SIMULATOR_NAME_PREFIX}-{i}"
            subprocess.run(
                ["xcrun", "simctl", "delete", sim_name],
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                ["xcrun", "simctl", "create", sim_name, device_name],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"xcrun simctl create failed for '{sim_name}': {result.stderr.strip()}"
                )
            udid = result.stdout.strip()

            subprocess.run(
                ["xcrun", "simctl", "boot", udid],
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["xcrun", "simctl", "bootstatus", udid, "-b"],
                capture_output=True,
                text=True,
            )
            with created_lock:
                created += 1
                logger.info(
                    "Created & booted simulator %s (%s) [%d/%d]",
                    sim_name,
                    udid,
                    created,
                    n,
                )
            return udid

        with ThreadPoolExecutor(max_workers=n) as pool:
            self._udids = list(pool.map(_create_one, range(n)))
        return self._udids

    def destroy(self) -> None:
        """Shut down and delete all simulators. No-op if pool is empty."""
        if not self._udids:
            return
        total = len(self._udids)
        destroyed = 0
        destroyed_lock = threading.Lock()

        def _delete_one(udid: str) -> None:
            nonlocal destroyed
            subprocess.run(
                ["xcrun", "simctl", "shutdown", udid], capture_output=True, text=True
            )
            subprocess.run(
                ["xcrun", "simctl", "delete", udid], capture_output=True, text=True
            )
            with destroyed_lock:
                destroyed += 1
                logger.info("Destroyed simulator %s (%d/%d)", udid, destroyed, total)

        with ThreadPoolExecutor(max_workers=total) as pool:
            list(pool.map(_delete_one, self._udids))
        self._udids = []

    @property
    def udids(self) -> list[str]:
        """Simulator UDIDs. Empty until create() is called."""
        return self._udids

    @staticmethod
    def _parse_device_name(test_destination: str) -> str:
        """Parse device name from destination string. Defaults to iPhone 16."""
        match = re.search(r"name=([^,]+)", test_destination)
        return match.group(1).strip() if match else DEFAULT_DEVICE_NAME
