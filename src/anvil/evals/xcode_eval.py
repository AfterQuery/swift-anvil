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
    _run_xcodebuild,
    inject_app_test_target,
    load_xcode_config,
    resolve_test_package_path,
)
from .xcode_parser import (
    merge_test_results,
    parse_build_result,
    parse_xcodebuild_output,
)

logger = logging.getLogger(__name__)

# Per-worker simulator UDID and index (thread-local for ThreadPoolExecutor).
_tls = threading.local()

# Lock that serialises the *start* of xcodebuild invocations so only one
# begins at a time (avoids Xcode build-service daemon deadlocks). Once the
# build is past the initial handshake (~2 s), the lock is released and the
# next worker can start. Much faster than the old fixed-delay stagger.
_build_start_lock: threading.Lock | None = None

# Default timeout for xcodebuild operations (seconds).
_DEFAULT_XCODEBUILD_TIMEOUT = 600

# Default max parallel xcodebuild workers.
_DEFAULT_MAX_WORKERS = 3

# Minimum gap between successive xcodebuild launches (seconds).
_BUILD_GATE_SECONDS = 1

# Timestamp of last xcodebuild launch (used with the lock above).
_last_build_start: float = 0.0


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
    """Extract device name from a destination string like
    ``platform=iOS Simulator,name=iPhone 17 Pro,OS=latest``."""
    match = re.search(r"name=([^,]+)", test_destination)
    return match.group(1).strip() if match else "iPhone 16"


def _create_simulator_pool(n: int, test_destination: str) -> list[str]:
    """Create *n* iOS Simulator clones for parallel test execution.

    Returns a list of simulator UDIDs.
    """
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
        # Wait for the simulator to finish booting before handing it to workers.
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
    """Detect whether a ``tests.swift`` targets the app module or SPM backend.

    Reads the first 10 lines looking for ``@testable import <module>``.
    Checks against ``app_test_module`` (the app's Swift module name) and
    ``app_test_scheme`` as fallback.  Returns ``"app"`` on match,
    ``"spm"`` otherwise (or when no app test config exists).
    """
    app_modules = set()
    for key in ("app_test_module", "app_test_scheme"):
        val = xcode_config.get(key, "")
        if val:
            app_modules.add(val)
    if not app_modules:
        return "spm"

    try:
        head = tests_file.read_text()[:500]
    except OSError:
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
    """Copy the task's ``tests.swift`` into the correct test target directory.

    Auto-detects whether the test targets the app module (``@testable import
    ACHNBrowserUI``) or the SPM backend (``@testable import Backend``) and
    routes accordingly.

    For app-level tests, also injects the ``ACHNBrowserUITests`` target into
    the Xcode project via :func:`inject_app_test_target` and adds the test
    file to the target's compile sources via ``pbxproj``.

    Returns ``"app"`` or ``"spm"`` if tests were copied, ``""`` if none.
    """
    if not source_tasks_dir:
        return ""

    parts = instance_id.split(".")
    task_name = parts[-1] if len(parts) > 1 else instance_id
    tests_file = source_tasks_dir / task_name / "tests.swift"

    if not tests_file.is_file():
        return ""

    test_type = _detect_test_type(tests_file, xcode_config)

    if test_type == "app":
        return _copy_app_tests(instance_id, tests_file, xcode_config, worktree_dir)
    else:
        return _copy_spm_tests(instance_id, tests_file, xcode_config, worktree_dir)


def _validate_pbxproj(worktree_dir: Path, project_rel: str) -> str | None:
    """Quick-validate that project.pbxproj is parseable after patch application.

    Returns an error message string if validation fails, or None if OK.
    Tries the ``pbxproj`` library first (structured parse), then falls back
    to ``plutil -lint`` (plist syntax check).
    """
    pbxproj_path = worktree_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        return None

    # 1) Try the pbxproj library (catches structural issues like duplicate
    #    UUIDs, bad group references, files in wrong build phases).
    if XcodeProject is not None:
        try:
            XcodeProject.load(str(pbxproj_path))
            return None
        except Exception as exc:
            return f"project.pbxproj parse error (pbxproj): {exc}"

    # 2) Fallback: plutil validates the plist syntax (catches missing braces,
    #    bad semicolons, etc. that make Xcode refuse to open the project).
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
    """Add a file to a pbxproj target's compile sources.

    *file_path* is the absolute path to the file on disk.
    *project_rel* is the xcodeproj path relative to the worktree root.
    """
    pbxproj_path = worktree_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        return

    if XcodeProject is None:
        logger.warning(
            "pbxproj not installed: cannot inject %s into target %s",
            file_path,
            target_name,
        )
        return

    try:
        project = XcodeProject.load(str(pbxproj_path))
        # rel_path must be relative to the xcodeproj's parent dir
        # (SOURCE_ROOT), not the worktree root — otherwise pbxproj
        # doubles the project subdirectory component.
        project_dir = (worktree_dir / project_rel).parent
        rel_path = str(file_path.relative_to(project_dir))
        project.add_file(rel_path, target_name=target_name)
        project.save()
        logger.info("Added %s to target %s in pbxproj", rel_path, target_name)
    except Exception as exc:
        logger.warning("pbxproj injection failed for %s: %s", target_name, exc)


def _propagate_pods_framework_paths(
    worktree_dir: Path,
    xcode_config: dict,
    test_target: str,
) -> None:
    """Copy CocoaPods framework search paths from the main target to the test target.

    Reads the main target's Pods xcconfig to discover framework directories,
    then patches the test target's build configurations in project.pbxproj so
    ``@testable import`` can resolve transitive module dependencies (e.g.
    Cache, CoreGPX) that are built as separate frameworks.
    """
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

    # Build the replacement FRAMEWORK_SEARCH_PATHS value.
    extra_paths = "".join(f'\n\t\t\t\t\t"$(BUILT_PRODUCTS_DIR)/{d}",' for d in pod_dirs)

    # Find and patch each build configuration block belonging to the test
    # target.  Configurations are linked via buildConfigurationList; rather
    # than full pbxproj parsing, we locate the test target's config UUIDs and
    # inject paths into their FRAMEWORK_SEARCH_PATHS arrays.
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
            # No existing FRAMEWORK_SEARCH_PATHS — inject one
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
    """Copy tests into the app-level test target.

    Injects the test target into the project if it doesn't exist, then copies
    the test file and adds it to the target's compile sources via pbxproj.
    """
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


def _run_xcodebuild_tests(
    cmd_info: tuple[list[str], Path] | None,
    timeout: int,
) -> dict | None:
    """Run an xcodebuild test command and parse the output.

    Returns parsed test output dict with ``_stdout``/``_stderr`` keys,
    or ``None`` when *cmd_info* is ``None``.
    """
    if not cmd_info:
        return None

    test_cmd, test_cwd = cmd_info
    test_result = _run_xcodebuild(test_cmd, str(test_cwd), timeout)
    output = parse_xcodebuild_output(test_result.stdout, test_result.stderr)
    if test_result.returncode != 0 and not output["tests"]:
        output = {
            "tests": [
                {
                    "name": "xctest_run",
                    "status": "FAILED",
                    "message": "xcodebuild test exited with non-zero",
                }
            ]
        }
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
) -> dict | None:
    """Run app-level unit tests via build-for-testing then test-without-building.

    Splitting the monolithic ``xcodebuild test`` into two phases avoids
    deadlocks when multiple parallel workers hit the Xcode build-service
    daemon simultaneously, and gives each phase its own timeout.
    """
    app_test_dd = worktree_dir / "DerivedData-app-tests"
    cmd_info = _build_xcodebuild_app_test_cmd(
        xcode_config,
        worktree_dir,
        app_test_dd,
    )
    if not cmd_info:
        return None

    test_cmd, test_cwd = cmd_info

    # Phase 1: build-for-testing — gate the start to avoid daemon deadlocks.
    _gate_build_start()

    build_cmd = _as_build_for_testing(test_cmd)
    build_result = _run_xcodebuild(
        build_cmd, str(test_cwd), _DEFAULT_XCODEBUILD_TIMEOUT
    )

    if build_result.returncode != 0:
        output = parse_xcodebuild_output(build_result.stdout, build_result.stderr)
        if not output["tests"]:
            output = {
                "tests": [
                    {
                        "name": "xctest_run",
                        "status": "FAILED",
                        "message": "xcodebuild build-for-testing failed",
                    }
                ]
            }
        output["_stdout"] = build_result.stdout
        output["_stderr"] = build_result.stderr
        return output

    # Phase 2: test-without-building (fast, no build contention).
    run_cmd = ["test-without-building" if c == "test" else c for c in test_cmd]
    test_result = _run_xcodebuild(run_cmd, str(test_cwd), _DEFAULT_XCODEBUILD_TIMEOUT)

    output = parse_xcodebuild_output(test_result.stdout, test_result.stderr)
    if test_result.returncode != 0 and not output["tests"]:
        output = {
            "tests": [
                {
                    "name": "xctest_run",
                    "status": "FAILED",
                    "message": "xcodebuild test exited with non-zero",
                }
            ]
        }
    output["_stdout"] = build_result.stdout + "\n" + test_result.stdout
    output["_stderr"] = build_result.stderr + "\n" + test_result.stderr
    return output


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
    """Evaluate a single patch using local xcodebuild.

    When *source_tasks_dir* is provided, copies the task's ``tests.swift``
    into the test target directory configured by ``test_files_dest`` in
    xcode_config.  If task tests are present they are **always** run, even
    when *compile_only* is ``True``.

    Returns {"tests": [{"name": ..., "status": ...}, ...]} or None on error.
    """
    tag = f"{instance_id}:{attempt}" if attempt else instance_id
    worktree_dir = None

    try:
        # Use a temp dir on the same APFS volume as the xcode cache so that
        # cp -c (clonefile) works for DerivedData.
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
                    return {
                        "tests": [
                            {
                                "name": "patch_apply",
                                "status": "FAILED",
                                "message": err_detail,
                            }
                        ]
                    }

        # Fast-fail if the patch corrupted project.pbxproj (avoids a slow
        # xcodebuild invocation that would fail with a cryptic parse error).
        # Only validate when the patch actually touches the project file —
        # an unmodified pbxproj from the cache is always valid.
        project_rel = xcode_config.get("project", "")
        if project_rel and "project.pbxproj" in patch:
            pbxproj_error = _validate_pbxproj(worktree_dir, project_rel)
            if pbxproj_error:
                logger.warning(
                    "pbxproj validation failed for %s: %s", tag, pbxproj_error
                )
                result = {
                    "tests": [
                        {
                            "name": "pbxproj_validation",
                            "status": "FAILED",
                            "message": pbxproj_error[:500],
                        }
                    ]
                }
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

        all_stdout = ""
        all_stderr = ""
        dd_dir = worktree_dir / "DerivedData"

        spm_standalone = test_type == "spm" and resolve_test_package_path(
            xcode_config, worktree_dir
        )

        if test_type == "app" or spm_standalone:
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

        # Override test_destination with per-worker simulator when running
        # inside a pool with dedicated simulators (avoids boot conflicts).
        test_xcode_config = xcode_config
        sim_udid = getattr(_tls, "sim_udid", None)
        if sim_udid:
            test_xcode_config = {
                **xcode_config,
                "test_destination": f"platform=iOS Simulator,id={sim_udid}",
                "app_test_destination": f"platform=iOS Simulator,id={sim_udid}",
            }

        xctest_output = None
        if run_tests:
            if test_type == "app":
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
                xctest_output = {
                    "tests": [
                        {
                            "name": "unit_test_setup",
                            "status": "FAILED",
                            "message": "Task tests found but test config "
                            "not configured in xcode_config.yaml",
                        }
                    ]
                }

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
        result = {
            "tests": [
                {
                    "name": "compilation",
                    "status": "FAILED",
                    "message": f"Build timed out ({timeout_s}s)",
                }
            ]
        }
        _save_eval_output(
            output_dir, instance_id, attempt, eval_id, result, patch, "", ""
        )
        return result
    except Exception as e:
        logger.error("Error evaluating %s: %s", tag, e, exc_info=True)
        error_msg = f"{type(e).__name__}: {e}"
        result = {
            "tests": [
                {
                    "name": "eval_infrastructure",
                    "status": "FAILED",
                    "message": error_msg[:500],
                }
            ]
        }
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


def _make_empty_patch_result(has_tests: bool) -> dict:
    """Return a synthetic FAILED result for an empty/blank patch."""
    if has_tests:
        return {
            "tests": [
                {
                    "name": "patch_content",
                    "status": "FAILED",
                    "message": "Empty patch — skipped build (tests would fail on unpatched base)",
                }
            ]
        }
    return {
        "tests": [
            {
                "name": "patch_content",
                "status": "FAILED",
                "message": "Empty patch — nothing to evaluate",
            }
        ]
    }


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
    """Run Xcode-based evaluation for a batch of patches.

    Args:
        patches: List of dicts with instance_id, patch, attempt keys.
        instances: Instance definitions from instances.yaml.
        dataset_tasks_dir: Path to dataset's tasks/ directory.
        output_dir: Base output directory for results.
        eval_id: Evaluation identifier prefix.
        max_workers: Max concurrent xcodebuild processes.
        compile_only: If True, only check compilation (skip repo-wide tests).
            Per-task unit tests are still run when present.
        dataset_id: Dataset identifier for config lookup.

    Returns:
        Dict mapping "instance_id:attempt_N" to bool pass/fail.
    """
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

    # Pre-compute which instance IDs have tests to avoid repeated filesystem stats.
    _has_tests_cache: dict[str, bool] = {}
    if src_tasks is not None:
        for ps in patches:
            iid = ps["instance_id"]
            if iid not in _has_tests_cache:
                task_name = iid.split(".")[-1]
                _has_tests_cache[iid] = (
                    src_tasks / task_name / "tests.swift"
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
    typer.echo(
        f"Running Xcode evals ({len(real_patches)} patches, {max_workers} workers, "
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
        and test_destination
        and "generic/" not in test_destination
    )

    # Initialise the build-start gate for this run.
    _build_start_lock = threading.Lock()

    passed_count = 0
    sim_udids: list[str] = []
    try:
        if needs_sim_pool:
            typer.echo(
                f"Creating {max_workers} simulators for parallel test execution..."
            )
            sim_udids = _create_simulator_pool(max_workers, test_destination)

        def _assign_sim_and_run(patch_sample: dict) -> dict:
            """Thread-pool wrapper that assigns a per-thread simulator."""
            if sim_udids:
                idx = threading.current_thread()._anvil_idx  # type: ignore[attr-defined]
                _tls.sim_udid = sim_udids[idx]
                _tls.worker_index = idx
            return eval_single_patch(
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

        # Assign stable simulator indices to thread-pool threads.
        _thread_idx = 0
        _thread_idx_lock = threading.Lock()

        def _thread_initializer():
            nonlocal _thread_idx
            with _thread_idx_lock:
                idx_val = _thread_idx
                _thread_idx += 1
            threading.current_thread()._anvil_idx = idx_val  # type: ignore[attr-defined]

        actual_workers = min(max_workers, len(real_patches))
        with ThreadPoolExecutor(
            max_workers=actual_workers,
            initializer=_thread_initializer,
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
                    output = {
                        "tests": [
                            {
                                "name": "eval_infrastructure",
                                "status": "FAILED",
                                "message": f"Worker error: {type(e).__name__}: {e}"[
                                    :500
                                ],
                            }
                        ]
                    }
                    worker_crashed = True

                tests = output.get("tests", [])
                failed = [t for t in tests if t["status"] == "FAILED"]
                passed_this = len(tests) > 0 and len(failed) == 0
                eval_results[result_key] = passed_this
                if passed_this:
                    passed_count += 1

                # Propagate result to deduplicated siblings.
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
    typer.echo(f"Xcode eval complete: {passed_count}/{len(eval_results)} passed")

    return eval_results


def validate_task_tests(
    dataset_id: str,
    max_workers: int | None = None,
) -> int:
    """Run task tests against the unpatched base commit and check consistency.

    Tests are categorized by **class name**:

    * Classes containing ``F2P`` (e.g. ``AnvilTask1F2PTests``) —
      **fail-to-pass**; must fail on base.
    * Everything else (repo tests, ``Anvil*P2P*``, etc.) —
      **pass-to-pass**; must pass on base.

    Reports inconsistencies: f2p tests that pass or p2p tests that fail.

    Returns 0 if all tests behave as expected, 1 on inconsistencies or
    infrastructure errors.
    """
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
        task_name = iid.split(".")[-1]
        if (src_tasks / task_name / "tests.swift").is_file():
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

    # Determine if we need a simulator pool for parallel execution.
    test_destination = xcode_config.get(
        "test_destination",
        xcode_config.get("destination", ""),
    )
    needs_sim_pool = (
        max_workers > 1 and test_destination and "generic/" not in test_destination
    )

    global _build_start_lock

    sim_udids: list[str] = []
    collected: list[tuple[str, dict | None]] = []
    _build_start_lock = threading.Lock()

    try:
        if needs_sim_pool:
            typer.echo(f"Creating {max_workers} simulators for parallel validation...")
            sim_udids = _create_simulator_pool(max_workers, test_destination)

        _thread_idx = 0
        _thread_idx_lock = threading.Lock()

        def _thread_init():
            nonlocal _thread_idx
            with _thread_idx_lock:
                idx = _thread_idx
                _thread_idx += 1
            threading.current_thread()._anvil_idx = idx  # type: ignore[attr-defined]

        def _validate_one(inst: dict) -> tuple[str, dict | None]:
            iid = inst["instance_id"]
            task_name = iid.split(".")[-1]
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
            initializer=_thread_init,
        ) as pool:
            future_to_task: dict = {}
            for inst in tasks_with_tests:
                future = pool.submit(_validate_one, inst)
                future_to_task[future] = inst["instance_id"].split(".")[-1]

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

    # Sort results by task name for deterministic output.
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
