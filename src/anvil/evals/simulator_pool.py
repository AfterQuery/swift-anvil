from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .constants import DEFAULT_DEVICE_NAME, SIMULATOR_NAME_PREFIX
from .xcode_cache import get_app_bundle_name, get_app_test_destination

logger = logging.getLogger(__name__)


def booted_udid_for_name(device_name: str) -> str | None:
    """Return the UDID of a booted simulator matching *device_name*, or None."""
    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "booted", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    try:
        devices = json.loads(result.stdout).get("devices", {})
        for runtime_devices in devices.values():
            for dev in runtime_devices:
                if dev.get("name") == device_name and dev.get("state") == "Booted":
                    return dev["udid"]
    except Exception:
        pass
    return None


def prewarm_app_binary(xcode_config: dict, products_dir: Path) -> None:
    """Launch and terminate the app to warm the binary page cache on the simulator."""
    dest = get_app_test_destination(xcode_config)
    m = re.search(r"\bid=([A-F0-9-]{36})\b", dest, re.IGNORECASE)
    if m:
        sim_udid = m.group(1)
    else:
        sim_udid = booted_udid_for_name(SimulatorPool._parse_device_name(dest))
        if not sim_udid:
            return

    app_bundle_name = get_app_bundle_name(xcode_config)
    app_bundle: Path | None = None
    if app_bundle_name and products_dir.exists():
        candidates = list(products_dir.glob(f"**/{app_bundle_name}.app"))
        if candidates:
            app_bundle = candidates[0]

    if not app_bundle or not app_bundle.exists():
        logger.debug("Pre-warm skipped: app bundle not found in %s", products_dir)
        return

    info_plist = app_bundle / "Info.plist"
    if not info_plist.exists():
        return

    result = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print CFBundleIdentifier", str(info_plist)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    bundle_id = result.stdout.strip()
    if not bundle_id:
        return

    logger.info("Pre-warming app %s on simulator %s", bundle_id, sim_udid)
    subprocess.run(
        ["xcrun", "simctl", "launch", sim_udid, bundle_id],
        capture_output=True,
        text=True,
        timeout=20,
    )
    time.sleep(2)
    subprocess.run(
        ["xcrun", "simctl", "terminate", sim_udid, bundle_id],
        capture_output=True,
        text=True,
        timeout=10,
    )


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
        """Parse device name from destination string. Defaults to iPhone 17 Pro."""
        match = re.search(r"name=([^,]+)", test_destination)
        return match.group(1).strip() if match else DEFAULT_DEVICE_NAME
