from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer
from ruamel.yaml import YAML
from tqdm import tqdm

try:
    from pbxproj import XcodeProject
except ImportError:
    XcodeProject = None

from ..config import repo_root, source_tasks_dir
from .xcode_cache import (
    XcodeBuildCache,
    _as_build_for_testing,
    _build_xcodebuild_app_test_cmd,
    _build_xcodebuild_cmd,
    _build_xcodebuild_test_cmd,
    _pbx_uuid,
    _run_xcodebuild,
    inject_app_test_target,
    inject_ui_test_target,
    load_xcode_config,
    resolve_test_package_path,
)
from .xcode_parser import (
    merge_test_results,
    parse_build_result,
    parse_xcodebuild_output,
)

logger = logging.getLogger(__name__)


def _task_name(instance_id: str) -> str:
    """Extract the task name from a dotted instance ID (e.g. ``"Repo.task-4"`` → ``"task-4"``)."""
    return instance_id.split(".")[-1]


# Per-worker simulator UDID (thread-local for ThreadPoolExecutor).
_tls = threading.local()

# Serialises xcodebuild starts to avoid build-service daemon deadlocks.
_build_start_lock: threading.Lock | None = None

_DEFAULT_XCODEBUILD_TIMEOUT = 600
_DEFAULT_MAX_WORKERS = 3
_BUILD_GATE_SECONDS = 1
_last_build_start: float = 0.0


def _make_thread_index_initializer():
    """Create a thread initializer that assigns _anvil_idx to each worker thread."""
    thread_idx = [0]
    lock = threading.Lock()

    def _init():
        with lock:
            idx = thread_idx[0]
            thread_idx[0] += 1
        threading.current_thread()._anvil_idx = idx  # type: ignore[attr-defined]

    return _init


def _gate_build_start() -> None:
    """Acquire the build gate, ensuring a minimum gap between xcodebuild starts."""
    global _last_build_start
    if _build_start_lock is None:
        return
    with _build_start_lock:
        now = time.monotonic()
        wait = _BUILD_GATE_SECONDS - (now - _last_build_start)
        if wait > 0:
            time.sleep(wait)
        _last_build_start = time.monotonic()


def _parse_device_name(test_destination: str) -> str:
    """Extract the device name from a destination string."""
    match = re.search(r"name=([^,]+)", test_destination)
    return match.group(1).strip() if match else "iPhone 16"


def _create_simulator_pool(n: int, test_destination: str) -> list[str]:
    """Create n iOS Simulator clones for parallel test execution. Returns UDIDs."""
    device_name = _parse_device_name(test_destination)
    created = 0
    created_lock = threading.Lock()

    def _create_one(i: int) -> str:
        nonlocal created
        sim_name = f"anvil-eval-{i}"
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
        udids = list(pool.map(_create_one, range(n)))
    return udids


def _destroy_simulator_pool(udids: list[str]) -> None:
    """Delete simulators created by :func:`_create_simulator_pool`."""
    total = len(udids)
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
        list(pool.map(_delete_one, udids))


def _detect_test_type(tests_file: Path, xcode_config: dict) -> str:
    """Return "ui", "app", or "spm" based on the test file's imports."""
    try:
        head = tests_file.read_text()[:500]
    except OSError:
        return "spm"

    if xcode_config.get("ui_test_target") and "XCUIApplication" in head:
        return "ui"

    app_modules = set()
    for key in ("app_test_module", "app_test_scheme"):
        val = xcode_config.get(key, "")
        if val:
            app_modules.add(val)
    if not app_modules:
        return "spm"

    for line in head.splitlines()[:10]:
        stripped = line.strip()
        if stripped.startswith("@testable import"):
            for mod in app_modules:
                if mod in stripped:
                    return "app"
    return "spm"


def _copy_task_tests(
    instance_id: str,
    source_tasks_dir: Path | None,
    xcode_config: dict,
    worktree_dir: Path,
) -> str:
    """Copy tests.swift into the correct target. Returns "ui"/"app"/"spm" or ""."""
    if not source_tasks_dir:
        return ""

    tests_file = source_tasks_dir / _task_name(instance_id) / "tests.swift"

    if not tests_file.is_file():
        return ""

    test_type = _detect_test_type(tests_file, xcode_config)

    if test_type == "ui":
        return _copy_ui_tests(instance_id, tests_file, xcode_config, worktree_dir)
    elif test_type == "app":
        return _copy_app_tests(instance_id, tests_file, xcode_config, worktree_dir)
    else:
        return _copy_spm_tests(instance_id, tests_file, xcode_config, worktree_dir)


def _copy_task_uitests(
    instance_id: str,
    source_tasks_dir: Path | None,
    xcode_config: dict,
    worktree_dir: Path,
) -> bool:
    """Copy uitests.swift into the UI test target. Returns True if copied."""
    if not source_tasks_dir:
        return False

    uitests_file = source_tasks_dir / _task_name(instance_id) / "uitests.swift"

    if not uitests_file.is_file():
        return False

    result = _copy_ui_tests(instance_id, uitests_file, xcode_config, worktree_dir)
    return result == "ui"


def _validate_pbxproj(worktree_dir: Path, project_rel: str) -> str | None:
    """Validate project.pbxproj after patch application. Returns error string or None."""
    pbxproj_path = worktree_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        return None

    if XcodeProject is not None:
        try:
            XcodeProject.load(str(pbxproj_path))
            return None
        except Exception as exc:
            return f"project.pbxproj parse error (pbxproj): {exc}"

    try:
        result = subprocess.run(
            ["plutil", "-lint", str(pbxproj_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return f"project.pbxproj plist validation failed: {detail}"
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("plutil validation skipped: %s", exc)

    return None


def _add_file_to_pbxproj(
    worktree_dir: Path,
    project_rel: str,
    file_path: Path,
    target_name: str,
) -> None:
    """Add a Swift source file to a pbxproj target's compile sources via string manipulation."""
    pbxproj_path = worktree_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        return

    file_name = file_path.name
    pbx = pbxproj_path.read_text()

    if file_name in pbx:
        logger.debug("File %s already in pbxproj, skipping", file_name)
        return

    file_ref_uuid = _pbx_uuid(f"{target_name}-{file_name}-fileref")
    build_uuid = _pbx_uuid(f"{target_name}-{file_name}-buildfile")

    pbx = pbx.replace(
        "/* End PBXBuildFile section */",
        f"\t\t{build_uuid} /* {file_name} in Sources */ = "
        f"{{isa = PBXBuildFile; fileRef = {file_ref_uuid} /* {file_name} */; }};\n"
        "/* End PBXBuildFile section */",
    )
    pbx = pbx.replace(
        "/* End PBXFileReference section */",
        f"\t\t{file_ref_uuid} /* {file_name} */ = {{isa = PBXFileReference; "
        f"lastKnownFileType = sourcecode.swift; path = {file_name}; "
        f'sourceTree = "<group>"; }};\n'
        "/* End PBXFileReference section */",
    )

    if m := re.search(
        rf"\w{{24}} /\* {re.escape(target_name)} \*/ = \{{\s*isa = PBXGroup;"
        rf".*?children = \((.*?)\);",
        pbx,
        re.DOTALL,
    ):
        pos = m.start(1) + len(m.group(1))
        pbx = pbx[:pos] + f"\n\t\t\t\t{file_ref_uuid} /* {file_name} */," + pbx[pos:]

    if (
        (
            m_target := re.search(
                rf"\w{{24}} /\* {re.escape(target_name)} \*/ = \{{\s*isa = PBXNativeTarget;"
                rf".*?buildPhases = \(([^)]*)\)",
                pbx,
                re.DOTALL,
            )
        )
        and (m_src := re.search(r"(\w{24}) /\* Sources \*/", m_target.group(1)))
        and (
            m_phase := re.search(
                rf"{re.escape(m_src.group(1))} /\* Sources \*/ = \{{.*?files = \(([^)]*)\)",
                pbx,
                re.DOTALL,
            )
        )
    ):
        pos = m_phase.start(1) + len(m_phase.group(1))
        pbx = (
            pbx[:pos]
            + f"\n\t\t\t\t{build_uuid} /* {file_name} in Sources */,"
            + pbx[pos:]
        )

    pbxproj_path.write_text(pbx)
    logger.info("Added %s to target %s in pbxproj", file_name, target_name)


def _propagate_pods_framework_paths(
    worktree_dir: Path,
    xcode_config: dict,
    test_target: str,
) -> None:
    """Copy CocoaPods FRAMEWORK_SEARCH_PATHS from the main target to the test target."""
    scheme = xcode_config.get("scheme", "")
    project_rel = xcode_config.get("project", "")
    if not scheme or not project_rel:
        return

    pods_xcconfig = (
        worktree_dir
        / "Pods"
        / "Target Support Files"
        / f"Pods-{scheme}"
        / f"Pods-{scheme}.debug.xcconfig"
    )
    if not pods_xcconfig.exists():
        return

    try:
        xcconfig_text = pods_xcconfig.read_text()
    except OSError:
        return

    pod_dirs = re.findall(r'PODS_CONFIGURATION_BUILD_DIR\}/([^\s"]+)', xcconfig_text)
    if not pod_dirs:
        return

    pbxproj_path = worktree_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        return

    try:
        pbx = pbxproj_path.read_text()
    except OSError:
        return

    extra_paths = "".join(f'\n\t\t\t\t\t"$(BUILT_PRODUCTS_DIR)/{d}",' for d in pod_dirs)

    target_match = re.search(
        rf"(/\* {re.escape(test_target)} \*/ = \{{.*?buildConfigurationList = )(\w{{24}})",
        pbx,
        re.DOTALL,
    )
    if not target_match:
        return

    config_list_uuid = target_match.group(2)
    config_list_match = re.search(
        rf"{config_list_uuid}.*?buildConfigurations = \((.*?)\)",
        pbx,
        re.DOTALL,
    )
    if not config_list_match:
        return

    config_uuids = re.findall(r"(\w{24})", config_list_match.group(1))

    modified = False
    for uuid in config_uuids:
        # Find the FRAMEWORK_SEARCH_PATHS in this config block
        pattern = (
            rf"({uuid}\s*/\*.*?\*/\s*=\s*\{{.*?"
            r"FRAMEWORK_SEARCH_PATHS\s*=\s*\()(.*?\);)"
        )
        m = re.search(pattern, pbx, re.DOTALL)
        if m:
            pbx = pbx[: m.start(2)] + extra_paths + m.group(2) + pbx[m.end(2) :]
            modified = True
        else:
            cfg_pattern = (
                rf"({uuid}\s*/\*.*?\*/\s*=\s*\{{[^{{}}]*?buildSettings\s*=\s*\{{)"
            )
            cm = re.search(cfg_pattern, pbx, re.DOTALL)
            if cm:
                insert = (
                    f"\n\t\t\t\tFRAMEWORK_SEARCH_PATHS = ("
                    f'\n\t\t\t\t\t"$(inherited)",'
                    f"{extra_paths}"
                    f"\n\t\t\t\t);"
                )
                pbx = pbx[: cm.end()] + insert + pbx[cm.end() :]
                modified = True

    if modified:
        pbxproj_path.write_text(pbx)
        logger.info(
            "Propagated %d Pods framework paths to %s", len(pod_dirs), test_target
        )


def _copy_spm_tests(
    instance_id: str,
    tests_file: Path,
    xcode_config: dict,
    worktree_dir: Path,
) -> str:
    """Copy tests into the SPM test target (existing Backend path)."""
    test_files_dest = xcode_config.get("test_files_dest", "")
    if not test_files_dest:
        return ""

    resolved_pkg = resolve_test_package_path(xcode_config, worktree_dir)
    has_pkg_config = bool(xcode_config.get("test_package_path"))

    if has_pkg_config and not resolved_pkg:
        logger.warning(
            "No test_package_path candidate found at worktree — skipping test copy for %s",
            instance_id,
        )
        return ""

    if resolved_pkg:
        dest_dir = worktree_dir / resolved_pkg / test_files_dest
    else:
        dest_dir = worktree_dir / test_files_dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    dst = dest_dir / tests_file.name
    shutil.copy2(str(tests_file), str(dst))
    logger.info("Copied test file %s → %s (spm)", tests_file.name, dest_dir)

    test_target = xcode_config.get("test_target", "")
    project_rel = xcode_config.get("project", "")
    if test_target and not xcode_config.get("test_package_path") and project_rel:
        _add_file_to_pbxproj(worktree_dir, project_rel, dst, test_target)
        _propagate_pods_framework_paths(worktree_dir, xcode_config, test_target)

    return "spm"


def _copy_app_tests(
    instance_id: str,
    tests_file: Path,
    xcode_config: dict,
    worktree_dir: Path,
) -> str:
    """Copy tests into the app-level test target, injecting it into the project if needed."""
    app_test_target = xcode_config.get("app_test_target", "")
    app_test_files_dest = xcode_config.get("app_test_files_dest", "")
    if not app_test_target or not app_test_files_dest:
        logger.warning(
            "app_test_target/app_test_files_dest not configured — falling back to spm"
        )
        return _copy_spm_tests(instance_id, tests_file, xcode_config, worktree_dir)

    inject_app_test_target(xcode_config, worktree_dir)

    dest_dir = worktree_dir / app_test_files_dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    dst = dest_dir / tests_file.name
    shutil.copy2(str(tests_file), str(dst))
    logger.info("Copied test file %s → %s (app)", tests_file.name, dest_dir)

    project_rel = xcode_config.get("project", "")
    if project_rel:
        _add_file_to_pbxproj(worktree_dir, project_rel, dst, app_test_target)

    return "app"


def _copy_ui_tests(
    instance_id: str,
    tests_file: Path,
    xcode_config: dict,
    worktree_dir: Path,
) -> str:
    """Copy tests into the UI test target, injecting it into the project if needed."""
    ui_test_target = xcode_config.get("ui_test_target", "")
    ui_test_files_dest = xcode_config.get("ui_test_files_dest", "")
    if not ui_test_target or not ui_test_files_dest:
        logger.warning(
            "ui_test_target/ui_test_files_dest not configured for %s", instance_id
        )
        return ""

    inject_ui_test_target(xcode_config, worktree_dir)

    dest_dir = worktree_dir / ui_test_files_dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    dst = dest_dir / tests_file.name
    shutil.copy2(str(tests_file), str(dst))
    logger.info("Copied test file %s → %s (ui)", tests_file.name, dest_dir)

    project_rel = xcode_config.get("project", "")
    if project_rel:
        _add_file_to_pbxproj(worktree_dir, project_rel, dst, ui_test_target)

    return "ui"


def _as_ui_test_config(xcode_config: dict) -> dict:
    """Map ui_test_* keys → app_test_* so run/cache functions work for UI tests."""
    overrides: dict = {}
    for ui_key, app_key in (
        ("ui_test_scheme", "app_test_scheme"),
        ("ui_test_target", "app_test_target"),
        ("ui_test_files_dest", "app_test_files_dest"),
        ("ui_test_destination", "app_test_destination"),
        ("ui_test_bundle_id", "app_test_bundle_id"),
    ):
        val = xcode_config.get(ui_key)
        if val is not None:
            overrides[app_key] = val
    return {**xcode_config, **overrides}


def _run_xcodebuild_tests(
    cmd_info: tuple[list[str], Path] | None,
    timeout: int,
) -> dict | None:
    """Run an xcodebuild test command and return parsed output, or None if cmd_info is None."""
    if not cmd_info:
        return None

    test_cmd, test_cwd = cmd_info
    test_result = _run_xcodebuild(test_cmd, str(test_cwd), timeout)
    output = parse_xcodebuild_output(test_result.stdout, test_result.stderr)
    if test_result.returncode != 0 and not output["tests"]:
        output = _failed_test_result(
            "xctest_run", "xcodebuild test exited with non-zero"
        )
    output["_stdout"] = test_result.stdout
    output["_stderr"] = test_result.stderr
    return output


def _run_spm_tests(
    xcode_config: dict,
    worktree_dir: Path,
    dd_dir: Path,
    spm_standalone: bool = False,
) -> dict | None:
    """Run SPM-based backend tests. Returns test output dict or None."""
    if spm_standalone or resolve_test_package_path(xcode_config, worktree_dir):
        test_dd = worktree_dir / "DerivedData-tests"
    else:
        test_dd = dd_dir
    return _run_xcodebuild_tests(
        _build_xcodebuild_test_cmd(xcode_config, worktree_dir, test_dd),
        timeout=_DEFAULT_XCODEBUILD_TIMEOUT,
    )


def _run_app_tests(
    xcode_config: dict,
    worktree_dir: Path,
    derived_data_dir: Path | None = None,
    is_ui_test: bool = False,
) -> dict | None:
    """Run app/UI tests via build-for-testing + test-without-building."""
    app_test_dd = derived_data_dir or worktree_dir / "DerivedData-app-tests"
    cmd_info = _build_xcodebuild_app_test_cmd(
        xcode_config,
        worktree_dir,
        app_test_dd,
    )
    if not cmd_info:
        return None

    test_cmd, test_cwd = cmd_info

    # Delete stale xctestrun so build-for-testing regenerates it with the injected UI test target.
    if is_ui_test:
        products_dir = app_test_dd / "Build" / "Products"
        if products_dir.exists():
            for stale in products_dir.glob("*.xctestrun"):
                stale.unlink()
                logger.info(
                    "Removed stale xctestrun before build-for-testing: %s", stale
                )

    _gate_build_start()

    build_cmd = _as_build_for_testing(test_cmd)
    build_result = _run_xcodebuild(
        build_cmd, str(test_cwd), _DEFAULT_XCODEBUILD_TIMEOUT
    )

    if build_result.returncode != 0:
        output = parse_xcodebuild_output(build_result.stdout, build_result.stderr)
        if not output["tests"]:
            output = _failed_test_result(
                "xctest_run", "xcodebuild build-for-testing failed"
            )
        output["_stdout"] = build_result.stdout
        output["_stderr"] = build_result.stderr
        return output

    run_cmd = ["test-without-building" if c == "test" else c for c in test_cmd]

    if is_ui_test:
        products_dir = app_test_dd / "Build" / "Products"
        _prewarm_app_binary(xcode_config, products_dir)
        xctestrun_files = (
            sorted(products_dir.glob("*.xctestrun")) if products_dir.exists() else []
        )
        if xctestrun_files:
            dest = xcode_config.get(
                "app_test_destination",
                xcode_config.get(
                    "test_destination", xcode_config.get("destination", "")
                ),
            )
            only_testing = xcode_config.get("app_test_target", "")
            run_cmd = [
                "xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(xctestrun_files[-1]),
                "-destination",
                dest,
            ]
            if only_testing:
                run_cmd.extend(["-only-testing", only_testing])
        else:
            app_name = xcode_config.get("app_bundle_name") or xcode_config.get(
                "scheme", ""
            )
            app_bundle: Path | None = None
            if app_name and products_dir.exists():
                candidates = list(products_dir.glob(f"**/{app_name}.app"))
                if candidates:
                    app_bundle = candidates[0]
            if app_bundle and app_bundle.exists():
                run_cmd.append(f"UITargetAppPath={app_bundle}")
            else:
                logger.warning(
                    "UITargetAppPath not found for UI tests in %s; "
                    "test-without-building may fail",
                    app_test_dd,
                )

    test_result = _run_xcodebuild(run_cmd, str(test_cwd), _DEFAULT_XCODEBUILD_TIMEOUT)

    output = parse_xcodebuild_output(test_result.stdout, test_result.stderr)
    if test_result.returncode != 0 and not output["tests"]:
        output = _failed_test_result(
            "xctest_run", "xcodebuild test exited with non-zero"
        )
    output["_stdout"] = build_result.stdout + "\n" + test_result.stdout
    output["_stderr"] = build_result.stderr + "\n" + test_result.stderr
    return output


def _booted_udid_for_name(device_name: str) -> str | None:
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


def _prewarm_app_binary(xcode_config: dict, products_dir: Path) -> None:
    """Launch and terminate the app to warm the binary page cache on the simulator."""
    dest = xcode_config.get(
        "app_test_destination", xcode_config.get("test_destination", "")
    )
    m = re.search(r"\bid=([A-F0-9-]{36})\b", dest, re.IGNORECASE)
    if m:
        sim_udid = m.group(1)
    else:
        sim_udid = _booted_udid_for_name(_parse_device_name(dest))
        if not sim_udid:
            return

    app_bundle_name = xcode_config.get("app_bundle_name") or xcode_config.get(
        "scheme", ""
    )
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


def _run_ui_tests(xcode_config: dict, worktree_dir: Path) -> dict | None:
    """Run UI tests reusing the cached DerivedData from the unit-test build."""
    ui_config = _as_ui_test_config(xcode_config)
    # Preserve the pool simulator UDID — _as_ui_test_config may overwrite it.
    if "id=" in xcode_config.get("app_test_destination", ""):
        ui_config = {
            **ui_config,
            "app_test_destination": xcode_config["app_test_destination"],
        }
    return _run_app_tests(ui_config, worktree_dir, is_ui_test=True)


def eval_single_patch(
    patch: str,
    instance_id: str,
    base_commit: str,
    repo_name: str,
    xcode_config: dict,
    cache: XcodeBuildCache,
    output_dir: Path,
    eval_id: str,
    attempt: int | None = None,
    compile_only: bool = False,
    source_tasks_dir: Path | None = None,
) -> dict | None:
    """Evaluate a single patch. Returns {"tests": [...]} or None on error."""
    tag = f"{instance_id}:{attempt}" if attempt else instance_id
    worktree_dir = None

    try:
        _tmp_base = Path(os.environ.get("ANVIL_TMPDIR", tempfile.gettempdir()))
        _tmp_base.mkdir(parents=True, exist_ok=True)
        worktree_dir = _tmp_base / (
            f"anvil-eval-{instance_id}-{attempt or 0}-{time.monotonic_ns()}"
        )

        cache.checkout(repo_name, base_commit, worktree_dir, xcode_config=xcode_config)

        if patch and patch.strip():
            apply_result = subprocess.run(
                ["git", "apply", "--allow-empty", "--ignore-whitespace"],
                cwd=str(worktree_dir),
                input=patch,
                capture_output=True,
                text=True,
            )
            if apply_result.returncode != 0:
                patch_file = worktree_dir / "_anvil_patch.diff"
                patch_file.write_text(patch)
                fallback = subprocess.run(
                    ["patch", "-p1", "-i", str(patch_file)],
                    cwd=str(worktree_dir),
                    capture_output=True,
                    text=True,
                )
                patch_file.unlink(missing_ok=True)
                if fallback.returncode != 0:
                    err_detail = fallback.stderr[:200] or fallback.stdout[:200]
                    logger.warning("Patch apply failed for %s: %s", tag, err_detail)
                    return _failed_test_result("patch_apply", err_detail)

        project_rel = xcode_config.get("project", "")
        if project_rel and "project.pbxproj" in patch:
            pbxproj_error = _validate_pbxproj(worktree_dir, project_rel)
            if pbxproj_error:
                logger.warning(
                    "pbxproj validation failed for %s: %s", tag, pbxproj_error
                )
                result = _failed_test_result("pbxproj_validation", pbxproj_error[:500])
                _save_eval_output(
                    output_dir,
                    instance_id,
                    attempt,
                    eval_id,
                    result,
                    patch,
                    "",
                    pbxproj_error,
                )
                return result

        test_type = _copy_task_tests(
            instance_id,
            source_tasks_dir,
            xcode_config,
            worktree_dir,
        )
        has_task_tests = bool(test_type)

        has_ui_tests = _copy_task_uitests(
            instance_id,
            source_tasks_dir,
            xcode_config,
            worktree_dir,
        )

        all_stdout = ""
        all_stderr = ""
        dd_dir = worktree_dir / "DerivedData"

        spm_standalone = test_type == "spm" and resolve_test_package_path(
            xcode_config, worktree_dir
        )

        if test_type in ("app", "ui") or spm_standalone:
            build_output = {"tests": []}
        else:
            _gate_build_start()

            build_cmd = _build_xcodebuild_cmd(
                xcode_config,
                worktree_dir,
                dd_dir,
                clean=False,
            )

            build_result = _run_xcodebuild(
                build_cmd, str(worktree_dir), _DEFAULT_XCODEBUILD_TIMEOUT
            )

            build_output = parse_build_result(
                build_result.returncode, build_result.stdout, build_result.stderr
            )
            all_stdout = build_result.stdout
            all_stderr = build_result.stderr

            if build_result.returncode != 0:
                _save_eval_output(
                    output_dir,
                    instance_id,
                    attempt,
                    eval_id,
                    build_output,
                    patch,
                    all_stdout,
                    all_stderr,
                )
                return build_output

        run_tests = has_task_tests or not compile_only

        run_config = (
            _as_ui_test_config(xcode_config) if test_type == "ui" else xcode_config
        )

        test_xcode_config = run_config
        sim_udid = getattr(_tls, "sim_udid", None)
        if sim_udid:
            test_xcode_config = {
                **run_config,
                "test_destination": f"platform=iOS Simulator,id={sim_udid}",
                "app_test_destination": f"platform=iOS Simulator,id={sim_udid}",
            }

        xctest_output = None
        if run_tests:
            if test_type in ("app", "ui"):
                xctest_output = _run_app_tests(test_xcode_config, worktree_dir)
            else:
                xctest_output = _run_spm_tests(
                    test_xcode_config,
                    worktree_dir,
                    dd_dir,
                    spm_standalone=bool(spm_standalone),
                )

            if xctest_output:
                all_stdout += "\n" + xctest_output.pop("_stdout", "")
                all_stderr += "\n" + xctest_output.pop("_stderr", "")

            if xctest_output is None and has_task_tests:
                xctest_output = _failed_test_result(
                    "unit_test_setup",
                    "Task tests found but test config not configured in xcode_config.yaml",
                )

            if has_ui_tests:
                ui_output = _run_ui_tests(test_xcode_config, worktree_dir)
                if ui_output:
                    all_stdout += "\n" + ui_output.pop("_stdout", "")
                    all_stderr += "\n" + ui_output.pop("_stderr", "")
                    xctest_output = (
                        merge_test_results(xctest_output, ui_output)
                        if xctest_output
                        else ui_output
                    )

        if xctest_output:
            combined = merge_test_results(build_output, xctest_output)
        else:
            combined = build_output

        _save_eval_output(
            output_dir,
            instance_id,
            attempt,
            eval_id,
            combined,
            patch,
            all_stdout,
            all_stderr,
        )
        return combined

    except subprocess.TimeoutExpired as te:
        timeout_s = te.timeout if te.timeout else "?"
        logger.error("Build/test timed out for %s after %ss", tag, timeout_s)
        result = _failed_test_result("compilation", f"Build timed out ({timeout_s}s)")
        _save_eval_output(
            output_dir, instance_id, attempt, eval_id, result, patch, "", ""
        )
        return result
    except Exception as e:
        logger.error("Error evaluating %s: %s", tag, e, exc_info=True)
        error_msg = f"{type(e).__name__}: {e}"
        result = _failed_test_result("eval_infrastructure", error_msg[:500])
        try:
            _save_eval_output(
                output_dir,
                instance_id,
                attempt,
                eval_id,
                result,
                patch,
                "",
                traceback.format_exc(),
            )
        except Exception:
            pass
        return result
    finally:
        if worktree_dir and worktree_dir.exists():
            try:
                cache.cleanup(repo_name, worktree_dir)
            except Exception:
                pass


def _save_eval_output(
    output_dir: Path,
    instance_id: str,
    attempt: int | None,
    eval_id: str,
    output: dict,
    patch: str,
    stdout: str,
    stderr: str,
) -> None:
    """Save eval outputs in the same directory structure as the Modal eval."""
    if attempt is not None:
        eval_dir = output_dir / instance_id / f"attempt_{attempt}" / "eval_results"
    else:
        eval_dir = output_dir / instance_id / "eval_results"

    eval_dir.mkdir(parents=True, exist_ok=True)

    prefix = eval_id
    (eval_dir / f"{prefix}_output.json").write_text(json.dumps(output, indent=2))
    (eval_dir / f"{prefix}_patch.diff").write_text(patch or "")
    (eval_dir / f"{prefix}_stdout.log").write_text(stdout or "")
    if stderr:
        (eval_dir / f"{prefix}_stderr.log").write_text(stderr)


def _failed_test_result(name: str, message: str) -> dict:
    """Return a synthetic FAILED test result dict."""
    return {"tests": [{"name": name, "status": "FAILED", "message": message}]}


def _make_empty_patch_result(has_tests: bool) -> dict:
    """Return a synthetic FAILED result for an empty/blank patch."""
    msg = (
        "Empty patch — skipped build (tests would fail on unpatched base)"
        if has_tests
        else "Empty patch — nothing to evaluate"
    )
    return _failed_test_result("patch_content", msg)


def run_xcode_evals(
    patches: list[dict],
    instances: list[dict],
    dataset_tasks_dir: Path,
    output_dir: Path,
    eval_id: str,
    max_workers: int | None = None,
    compile_only: bool = False,
    dataset_id: str | None = None,
) -> dict[str, bool]:
    """Run Xcode evals for a batch of patches. Returns instance_id → pass/fail."""
    global _build_start_lock

    xcode_config = load_xcode_config(dataset_tasks_dir, dataset_id=dataset_id)
    cache = XcodeBuildCache()

    instance_map = {inst["instance_id"]: inst for inst in instances}

    src_tasks: Path | None = None
    if dataset_id:
        candidate = source_tasks_dir(dataset_id)
        if candidate.is_dir():
            src_tasks = candidate

    if max_workers is None:
        max_workers = _DEFAULT_MAX_WORKERS

    eval_results: dict[str, bool] = {}

    _has_tests_cache: dict[str, bool] = {}
    if src_tasks is not None:
        for ps in patches:
            iid = ps["instance_id"]
            if iid not in _has_tests_cache:
                _has_tests_cache[iid] = (
                    src_tasks / _task_name(iid) / "tests.swift"
                ).is_file()

    def _has_tests(iid: str) -> bool:
        return _has_tests_cache.get(iid, False)

    real_patches: list[dict] = []
    skipped = 0
    dedup_map: dict[tuple, list[dict]] = {}  # (iid, patch_hash) -> [samples]

    for ps in patches:
        iid = ps["instance_id"]
        patch_text = ps.get("patch", ps.get("model_patch", ""))
        if not patch_text or not patch_text.strip():
            attempt = ps.get("attempt")
            result_key = f"{iid}:attempt_{attempt}" if attempt else iid
            eval_results[result_key] = False
            has_tests = _has_tests(iid)
            output = _make_empty_patch_result(has_tests)
            _save_eval_output(output_dir, iid, attempt, eval_id, output, "", "", "")
            if attempt is not None:
                task_results_dir = (
                    output_dir / iid / f"attempt_{attempt}" / "eval_results"
                )
                task_results_dir.mkdir(parents=True, exist_ok=True)
                (task_results_dir / "eval_results.json").write_text(
                    json.dumps({iid: False})
                )
            skipped += 1
            continue

        key = (iid, hash(patch_text))
        if key in dedup_map:
            dedup_map[key].append(ps)
        else:
            dedup_map[key] = [ps]
            real_patches.append(ps)

    if skipped:
        typer.echo(f"Skipped {skipped} empty patches (instant fail)")

    n_with_tests = sum(1 for p in real_patches if _has_tests(p["instance_id"]))
    actual_workers = min(max_workers, len(real_patches))
    typer.echo(
        f"Running Xcode evals ({len(real_patches)} patches, {actual_workers} workers, "
        f"compile_only={compile_only}, {n_with_tests} with unit tests)"
    )

    if not real_patches:
        (output_dir / "eval_results.json").write_text(json.dumps(eval_results))
        passed_count = sum(1 for v in eval_results.values() if v)
        typer.echo(f"Xcode eval complete: {passed_count}/{len(eval_results)} passed")
        return eval_results

    needs_tests = n_with_tests > 0 or not compile_only
    test_destination = xcode_config.get(
        "test_destination",
        xcode_config.get("destination", ""),
    )
    needs_sim_pool = (
        needs_tests
        and max_workers > 1
        and len(real_patches) > 1
        and test_destination
        and "generic/" not in test_destination
    )

    _build_start_lock = threading.Lock()

    passed_count = 0
    eval_durations: list[float] = []
    sim_udids: list[str] = []
    try:
        if needs_sim_pool:
            typer.echo(
                f"Creating {max_workers} simulators for parallel test execution..."
            )
            sim_udids = _create_simulator_pool(max_workers, test_destination)

        def _assign_sim_and_run(patch_sample: dict) -> dict:
            if sim_udids:
                idx = threading.current_thread()._anvil_idx  # type: ignore[attr-defined]
                _tls.sim_udid = sim_udids[idx]
                _tls.worker_index = idx
            t0 = time.time()
            result = eval_single_patch(
                patch=patch_sample.get("patch", patch_sample.get("model_patch", "")),
                instance_id=patch_sample["instance_id"],
                base_commit=instance_map[patch_sample["instance_id"]]["base_commit"],
                repo_name=instance_map[patch_sample["instance_id"]]["repo_name"],
                xcode_config=xcode_config,
                cache=cache,
                output_dir=output_dir,
                eval_id=eval_id,
                attempt=patch_sample.get("attempt"),
                compile_only=compile_only,
                source_tasks_dir=src_tasks,
            )
            eval_durations.append(time.time() - t0)
            return result

        with ThreadPoolExecutor(
            max_workers=actual_workers,
            initializer=_make_thread_index_initializer(),
        ) as pool:
            future_to_patch = {}
            for patch_sample in real_patches:
                future = pool.submit(_assign_sim_and_run, patch_sample)
                future_to_patch[future] = patch_sample

            pbar = tqdm(
                as_completed(future_to_patch),
                total=len(future_to_patch),
                desc="Xcode evals",
                unit="eval",
            )
            for future in pbar:
                patch_sample = future_to_patch[future]
                iid = patch_sample["instance_id"]
                attempt = patch_sample.get("attempt")
                result_key = f"{iid}:attempt_{attempt}" if attempt else iid

                worker_crashed = False
                try:
                    output = future.result()
                except Exception as e:
                    logger.error("Eval failed for %s: %s", result_key, e)
                    output = _failed_test_result(
                        "eval_infrastructure",
                        f"Worker error: {type(e).__name__}: {e}"[:500],
                    )
                    worker_crashed = True

                tests = output.get("tests", [])
                failed = [t for t in tests if t["status"] == "FAILED"]
                passed_this = len(tests) > 0 and len(failed) == 0
                eval_results[result_key] = passed_this
                if passed_this:
                    passed_count += 1

                patch_text = patch_sample.get(
                    "patch", patch_sample.get("model_patch", "")
                )
                dup_key = (iid, hash(patch_text))
                for sibling in dedup_map.get(dup_key, [])[1:]:
                    sib_attempt = sibling.get("attempt")
                    sib_key = f"{iid}:attempt_{sib_attempt}" if sib_attempt else iid
                    eval_results[sib_key] = passed_this
                    if passed_this:
                        passed_count += 1
                    if sib_attempt is not None:
                        sib_dir = (
                            output_dir / iid / f"attempt_{sib_attempt}" / "eval_results"
                        )
                        sib_dir.mkdir(parents=True, exist_ok=True)
                        (sib_dir / "eval_results.json").write_text(
                            json.dumps({iid: passed_this})
                        )

                if attempt is not None:
                    task_results_dir = (
                        output_dir / iid / f"attempt_{attempt}" / "eval_results"
                    )
                    task_results_dir.mkdir(parents=True, exist_ok=True)
                    (task_results_dir / "eval_results.json").write_text(
                        json.dumps({iid: eval_results[result_key]})
                    )
                    if worker_crashed:
                        (task_results_dir / f"{eval_id}_output.json").write_text(
                            json.dumps(output, indent=2)
                        )

                passed = passed_count
                total = len(eval_results)
                tag = f"{iid}:{attempt}" if attempt else iid
                status = "pass" if eval_results.get(result_key) else "fail"
                pbar.set_postfix_str(f"{passed}/{total} passed, {tag} {status}")
    finally:
        _build_start_lock = None
        if sim_udids:
            typer.echo(f"Cleaning up {len(sim_udids)} eval simulators...")
            _destroy_simulator_pool(sim_udids)

    (output_dir / "eval_results.json").write_text(json.dumps(eval_results))
    avg_s = sum(eval_durations) / len(eval_durations) if eval_durations else 0
    typer.echo(
        f"Xcode eval complete: {passed_count}/{len(eval_results)} passed"
        f"  |  avg eval time: {avg_s:.0f}s ({avg_s/60:.1f}m)"
    )

    return eval_results


def validate_task_tests(
    dataset_id: str,
    max_workers: int | None = None,
) -> int:
    """Run task tests on the unpatched base to verify f2p/p2p expectations. Returns 0 on success."""
    dataset_tasks_dir = repo_root() / dataset_id / "tasks"
    src_tasks = source_tasks_dir(dataset_id)

    if not dataset_tasks_dir.exists():
        typer.echo(f"Error: dataset tasks dir not found: {dataset_tasks_dir}")
        return 1

    xcode_config = load_xcode_config(dataset_tasks_dir, dataset_id=dataset_id)
    cache = XcodeBuildCache()

    instances = _load_instances_yaml(dataset_tasks_dir / "instances.yaml")

    tasks_with_tests = []
    for inst in instances:
        iid = inst["instance_id"]
        if (src_tasks / _task_name(iid) / "tests.swift").is_file():
            tasks_with_tests.append(inst)

    if not tasks_with_tests:
        typer.echo("No tasks with tests.swift found — nothing to validate.")
        return 0

    if max_workers is None:
        max_workers = min(len(tasks_with_tests), _DEFAULT_MAX_WORKERS)
    max_workers = min(max_workers, len(tasks_with_tests))

    typer.echo(
        f"Validating {len(tasks_with_tests)} task(s) on unpatched base commit "
        f"({max_workers} worker{'s' if max_workers > 1 else ''})"
    )
    typer.echo(
        "  (class name contains 'F2P' = fail-to-pass, all others = pass-to-pass)\n"
    )

    output_dir = Path(tempfile.mkdtemp(prefix="anvil-validate-"))

    test_destination = xcode_config.get(
        "test_destination",
        xcode_config.get("destination", ""),
    )
    needs_sim_pool = (
        max_workers > 1
        and len(tasks_with_tests) > 1
        and test_destination
        and "generic/" not in test_destination
    )

    global _build_start_lock

    sim_udids: list[str] = []
    collected: list[tuple[str, dict | None]] = []
    _build_start_lock = threading.Lock()

    try:
        if needs_sim_pool:
            typer.echo(f"Creating {max_workers} simulators for parallel validation...")
            sim_udids = _create_simulator_pool(max_workers, test_destination)

        def _validate_one(inst: dict) -> tuple[str, dict | None]:
            iid = inst["instance_id"]
            task_name = _task_name(iid)
            if sim_udids:
                idx = threading.current_thread()._anvil_idx  # type: ignore[attr-defined]
                _tls.sim_udid = sim_udids[idx]
                _tls.worker_index = idx
            try:
                result = eval_single_patch(
                    patch="",
                    instance_id=iid,
                    base_commit=inst["base_commit"],
                    repo_name=inst["repo_name"],
                    xcode_config=xcode_config,
                    cache=cache,
                    output_dir=output_dir,
                    eval_id="validate-base",
                    attempt=None,
                    compile_only=False,
                    source_tasks_dir=src_tasks,
                )
            except Exception as e:
                logger.error("Validation failed for %s: %s", task_name, e)
                result = None
            return (task_name, result)

        with ThreadPoolExecutor(
            max_workers=max_workers,
            initializer=_make_thread_index_initializer(),
        ) as pool:
            future_to_task: dict = {}
            for inst in tasks_with_tests:
                future = pool.submit(_validate_one, inst)
                future_to_task[future] = _task_name(inst["instance_id"])

            pbar = tqdm(
                as_completed(future_to_task),
                total=len(future_to_task),
                desc="Validating",
                unit="task",
            )
            for future in pbar:
                task_name, result = future.result()
                collected.append((task_name, result))
                pbar.set_postfix_str(task_name)
    finally:
        _build_start_lock = None
        if sim_udids:
            typer.echo(f"Cleaning up {len(sim_udids)} validation simulators...")
            _destroy_simulator_pool(sim_udids)

    collected.sort(key=lambda x: x[0])

    all_ok = True
    for task_name, result in collected:
        tests = result.get("tests", []) if result else []

        if not tests:
            typer.secho(
                f"  {task_name}: ERROR — no test results (infrastructure issue?)",
                fg=typer.colors.RED,
            )
            all_ok = False
            continue

        _synthetic = {"compilation", "xctest_run", "unit_test_setup", "patch_apply"}
        real_tests = [t for t in tests if t["name"] not in _synthetic]
        synthetic_failures = [
            t for t in tests if t["name"] in _synthetic and t["status"] == "FAILED"
        ]

        if not real_tests and not synthetic_failures:
            typer.secho(
                f"  {task_name}: OK — compile-only (no unit tests)",
                fg=typer.colors.GREEN,
            )
            continue

        if not real_tests and synthetic_failures:
            test_src = src_tasks / task_name / "tests.swift"
            has_f2p = (
                "F2P" in test_src.read_text().upper() if test_src.is_file() else False
            )
            if has_f2p:
                typer.secho(
                    f"  {task_name}: OK — tests failed to compile on base (f2p expected)",
                    fg=typer.colors.GREEN,
                )
            else:
                typer.secho(
                    f"  {task_name}: ERROR — tests failed to compile on base (no F2P classes — test bug?)",
                    fg=typer.colors.RED,
                )
                all_ok = False
            continue

        p2p_pass, p2p_fail = [], []
        f2p_pass, f2p_fail = [], []
        for t in real_tests:
            is_f2p = "F2P" in t.get("class_name", "").upper()
            passed = t["status"] == "PASSED"
            if is_f2p:
                (f2p_pass if passed else f2p_fail).append(t)
            else:
                (p2p_pass if passed else p2p_fail).append(t)

        issues = []
        if f2p_pass:
            issues.append(f"{len(f2p_pass)} f2p test(s) PASS (should fail)")
        if p2p_fail:
            issues.append(f"{len(p2p_fail)} p2p test(s) FAIL (should pass)")

        counts = []
        if f2p_fail:
            counts.append(f"{len(f2p_fail)} f2p fail")
        if f2p_pass:
            counts.append(f"{len(f2p_pass)} f2p pass")
        if p2p_pass:
            counts.append(f"{len(p2p_pass)} p2p pass")
        if p2p_fail:
            counts.append(f"{len(p2p_fail)} p2p fail")

        summary = ", ".join(counts)

        if issues:
            typer.secho(
                f"  {task_name}: ISSUE — {'; '.join(issues)}  ({summary})",
                fg=typer.colors.RED,
            )
            for t in f2p_pass:
                cls = t.get("class_name", "?")
                typer.echo(f"    f2p should fail: {cls}.{t['name']}")
            for t in p2p_fail:
                cls = t.get("class_name", "?")
                msg = t.get("message", "")
                typer.echo(
                    f"    p2p should pass: {cls}.{t['name']}{': ' + msg[:80] if msg else ''}"
                )
            all_ok = False
        else:
            typer.secho(f"  {task_name}: OK — {summary}", fg=typer.colors.GREEN)

    typer.echo("")
    shutil.rmtree(output_dir, ignore_errors=True)

    if all_ok:
        typer.secho(
            "All task tests consistent with expectations.", fg=typer.colors.GREEN
        )
        return 0
    else:
        typer.secho("Some tasks have inconsistencies — see above.", fg=typer.colors.RED)
        return 1


def _load_instances_yaml(path: Path) -> list[dict]:
    """Load instances from instances.yaml."""
    loader = YAML()
    return list(loader.load(path))
