from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer
from tqdm import tqdm

from ..config import source_tasks_dir
from .xcode_cache import (
    XcodeBuildCache,
    _as_build_for_testing,
    _build_timeout,
    _run_pre_build_commands,
    _build_xcodebuild_app_test_cmd,
    _build_xcodebuild_cmd,
    _build_xcodebuild_test_cmd,
    _run_xcodebuild,
    get_app_bundle_name,
    get_app_test_destination,
    get_test_destination,
    load_xcode_config,
    resolve_test_package_path,
)
from .xcode_parser import (
    format_xcode_failure_summary,
    merge_test_results,
    parse_build_result,
    parse_xcodebuild_output,
)
from .build_gate import (
    activate as activate_build_gate,
    deactivate as deactivate_build_gate,
    gate_build_start,
    make_thread_index_initializer,
)
from .constants import (
    DEFAULT_MAX_WORKERS,
    TESTS_FILENAME,
    XCODE_CONFIG_APP_TEST_TARGET,
    XCODE_CONFIG_PROJECT,
    OUTPUT_KEY_TESTS,
    TEST_NAME_COMPILATION,
    TEST_NAME_EVAL_INFRASTRUCTURE,
    TEST_NAME_PATCH_APPLY,
    TEST_NAME_PBXPROJ_VALIDATION,
    TEST_NAME_UNIT_TEST_SETUP,
    TEST_NAME_XCTEST_RUN,
    TEST_STATUS_FAILED,
    TEST_TYPE_APP,
    TEST_TYPE_SPM,
    TEST_TYPE_UI,
    UI_TO_APP_CONFIG_KEYS,
)
from .eval_output import (
    failed_test_result,
    make_empty_patch_result,
    save_eval_output,
)
from .simulator_pool import SimulatorPool, prewarm_app_binary
from .task_test_copier import TaskTestCopier

logger = logging.getLogger(__name__)

# Per-worker simulator UDID (thread-local for ThreadPoolExecutor).
_tls = threading.local()


def _get_patch_text(ps: dict) -> str:
    """Extract patch text from a patch sample dict (patch or model_patch key)."""
    return ps.get("patch", ps.get("model_patch", ""))


def _normalize_unified_diff_text(patch: str) -> str:
    """Strip trailing TABs from --- / +++ lines (some exporters add \\t before timestamp)."""
    parts: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("--- ") or line.startswith("+++ "):
            if line.endswith("\r\n"):
                line = line[:-2].rstrip("\t") + "\r\n"
            elif line.endswith("\n"):
                line = line[:-1].rstrip("\t") + "\n"
            else:
                line = line.rstrip("\t")
        parts.append(line)
    return "".join(parts)


def _patch_paths_contain_spaces(patch: str) -> bool:
    """True if any ---/+++ file path contains a space (before the tab timestamp)."""
    for line in patch.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            prefix = "--- a/" if line.startswith("--- a/") else "+++ b/"
            path_part = line[len(prefix) :].split("\t", 1)[0].strip()
            if " " in path_part:
                return True
    return False


def _result_key(iid: str, attempt: int | None) -> str:
    """Format result key for eval_results dict (e.g. 'task-1:attempt_2' or 'task-1')."""
    return f"{iid}:attempt_{attempt}" if attempt else iid


def _eval_results_dir(output_dir: Path, iid: str, attempt: int | None) -> Path:
    """Path to eval_results dir for an instance/attempt."""
    if attempt is not None:
        return output_dir / iid / f"attempt_{attempt}" / "eval_results"
    return output_dir / iid / "eval_results"


def _write_eval_result_json(
    output_dir: Path, iid: str, attempt: int | None, passed: bool
) -> None:
    """Write eval_results.json with {iid: passed} in the eval_results dir."""
    task_results_dir = _eval_results_dir(output_dir, iid, attempt)
    task_results_dir.mkdir(parents=True, exist_ok=True)
    (task_results_dir / "eval_results.json").write_text(json.dumps({iid: passed}))


def _as_ui_test_config(xcode_config: dict) -> dict:
    """Map ui_test_* keys → app_test_* so run/cache functions work for UI tests."""
    overrides: dict = {}
    for ui_key, app_key in UI_TO_APP_CONFIG_KEYS:
        val = xcode_config.get(ui_key)
        if val is not None:
            overrides[app_key] = val
    return {**xcode_config, **overrides}


def _finalize_test_output(
    xcode_result,
    failure_msg: str = "xcodebuild test exited with non-zero",
    extra_stdout: str = "",
    extra_stderr: str = "",
) -> dict:
    """Parse xcodebuild output, apply fallback failure if returncode is non-zero and no tests
    were recorded, then attach _stdout/_stderr (prefixed with any extra build output).
    """
    output = parse_xcodebuild_output(xcode_result.stdout, xcode_result.stderr)
    if xcode_result.returncode != 0 and not output[OUTPUT_KEY_TESTS]:
        detail = format_xcode_failure_summary(
            xcode_result.stdout,
            xcode_result.stderr,
            failure_msg,
        )
        output = failed_test_result(TEST_NAME_XCTEST_RUN, detail)
    output["_stdout"] = (
        (extra_stdout + "\n" + xcode_result.stdout)
        if extra_stdout
        else xcode_result.stdout
    )
    output["_stderr"] = (
        (extra_stderr + "\n" + xcode_result.stderr)
        if extra_stderr
        else xcode_result.stderr
    )
    return output


def _pop_stdout_stderr(
    result: dict, accumulated_stdout: str, accumulated_stderr: str
) -> tuple[str, str]:
    """Pop _stdout/_stderr from result dict and append to accumulated strings."""
    return (
        accumulated_stdout + "\n" + result.pop("_stdout", ""),
        accumulated_stderr + "\n" + result.pop("_stderr", ""),
    )


def _run_xcodebuild_tests(
    cmd_info: tuple[list[str], Path] | None,
    timeout: int,
) -> dict | None:
    """Run an xcodebuild test command and return parsed output, or None if cmd_info is None."""
    if not cmd_info:
        return None

    test_cmd, test_cwd = cmd_info
    test_result = _run_xcodebuild(test_cmd, str(test_cwd), timeout)
    return _finalize_test_output(test_result)


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
        timeout=_build_timeout(xcode_config),
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

    products_dir = app_test_dd / "Build" / "Products"

    # Delete stale xctestrun so build-for-testing regenerates it with the injected UI test target.
    if is_ui_test:
        if products_dir.exists():
            for stale in products_dir.glob("*.xctestrun"):
                stale.unlink()
                logger.info(
                    "Removed stale xctestrun before build-for-testing: %s", stale
                )

    gate_build_start()

    build_cmd = _as_build_for_testing(test_cmd)
    build_result = _run_xcodebuild(
        build_cmd, str(test_cwd), _build_timeout(xcode_config)
    )

    if build_result.returncode != 0:
        return _finalize_test_output(
            build_result, failure_msg="xcodebuild build-for-testing failed"
        )

    run_cmd = ["test-without-building" if c == "test" else c for c in test_cmd]

    if is_ui_test:
        prewarm_app_binary(xcode_config, products_dir)
        xctestrun_files = (
            sorted(products_dir.glob("*.xctestrun")) if products_dir.exists() else []
        )
        if xctestrun_files:
            dest = get_app_test_destination(xcode_config)
            only_testing = xcode_config.get(XCODE_CONFIG_APP_TEST_TARGET, "")
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
            app_name = get_app_bundle_name(xcode_config)
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

    test_result = _run_xcodebuild(run_cmd, str(test_cwd), _build_timeout(xcode_config))

    return _finalize_test_output(
        test_result,
        extra_stdout=build_result.stdout,
        extra_stderr=build_result.stderr,
    )


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
    run_ui_tests: bool = True,
) -> dict | None:
    """Evaluate a single patch. Returns {"tests": [...]} or None on error."""
    tag = _result_key(instance_id, attempt)
    worktree_dir = None

    try:
        worktree_dir = cache.eval_worktree_path(
            repo_name, base_commit, instance_id, attempt
        )

        cache.checkout(
            repo_name,
            base_commit,
            worktree_dir,
            xcode_config=xcode_config,
            copy_derived_data=False,
            run_pre_build=False,
        )

        if patch and patch.strip():
            patch = _normalize_unified_diff_text(patch)
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
                git_file_result = subprocess.run(
                    [
                        "git",
                        "apply",
                        "--allow-empty",
                        "--ignore-whitespace",
                        str(patch_file),
                    ],
                    cwd=str(worktree_dir),
                    capture_output=True,
                    text=True,
                )
                if git_file_result.returncode != 0:
                    # Third attempt: relax whitespace further (helps some .strings hunks).
                    git_ws = subprocess.run(
                        [
                            "git",
                            "apply",
                            "--allow-empty",
                            "--ignore-whitespace",
                            "--ignore-space-change",
                            str(patch_file),
                        ],
                        cwd=str(worktree_dir),
                        capture_output=True,
                        text=True,
                    )
                    if git_ws.returncode != 0:
                        # Last resort: patch(1) — but it cannot handle paths with spaces in
                        # the filename (e.g. "OpenGpxTracker-Watch Extension/...").
                        if _patch_paths_contain_spaces(patch):
                            err_detail = (
                                (git_ws.stderr or git_ws.stdout)
                                or (git_file_result.stderr or git_file_result.stdout)
                                or (apply_result.stderr or apply_result.stdout)
                            )[:500]
                            logger.warning(
                                "Patch apply failed for %s (git apply; paths contain spaces, "
                                "skipping patch fallback): %s",
                                tag,
                                err_detail[:200],
                            )
                            patch_file.unlink(missing_ok=True)
                            result = failed_test_result(
                                TEST_NAME_PATCH_APPLY, err_detail
                            )
                            combined_err = "\n".join(
                                filter(
                                    None,
                                    [
                                        apply_result.stderr,
                                        git_file_result.stderr,
                                        git_ws.stderr,
                                    ],
                                )
                            )
                            save_eval_output(
                                output_dir,
                                instance_id,
                                attempt,
                                eval_id,
                                result,
                                patch,
                                combined_err or err_detail,
                                "",
                            )
                            return result
                        fallback = subprocess.run(
                            ["patch", "-p1", "-l", "-i", str(patch_file)],
                            cwd=str(worktree_dir),
                            capture_output=True,
                            text=True,
                        )
                        if fallback.returncode != 0:
                            err_detail = fallback.stderr[:200] or fallback.stdout[:200]
                            logger.warning(
                                "Patch apply failed for %s: %s", tag, err_detail
                            )
                            patch_file.unlink(missing_ok=True)
                            result = failed_test_result(
                                TEST_NAME_PATCH_APPLY, err_detail
                            )
                            combined_err = "\n".join(
                                filter(
                                    None,
                                    [
                                        apply_result.stderr,
                                        git_file_result.stderr,
                                        git_ws.stderr,
                                        fallback.stderr,
                                    ],
                                )
                            )
                            save_eval_output(
                                output_dir,
                                instance_id,
                                attempt,
                                eval_id,
                                result,
                                patch,
                                combined_err or err_detail,
                                "",
                            )
                            return result
                patch_file.unlink(missing_ok=True)

        if xcode_config:
            _run_pre_build_commands(xcode_config, worktree_dir)

        project_rel = xcode_config.get(XCODE_CONFIG_PROJECT, "")
        if project_rel and "project.pbxproj" in patch:
            pbxproj_error = TaskTestCopier.validate_pbxproj(worktree_dir, project_rel)
            if pbxproj_error:
                logger.warning(
                    "pbxproj validation failed for %s: %s", tag, pbxproj_error
                )
                result = failed_test_result(
                    TEST_NAME_PBXPROJ_VALIDATION, pbxproj_error[:500]
                )
                save_eval_output(
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

        test_copier = TaskTestCopier(source_tasks_dir, xcode_config)
        test_type = test_copier.copy_task_tests(instance_id, worktree_dir)
        has_task_tests = bool(test_type)

        has_ui_tests = (
            test_copier.copy_task_uitests(instance_id, worktree_dir)
            if run_ui_tests
            else False
        )

        all_stdout = ""
        all_stderr = ""
        dd_dir = worktree_dir / "DerivedData"

        spm_standalone = test_type == TEST_TYPE_SPM and resolve_test_package_path(
            xcode_config, worktree_dir
        )

        if test_type in (TEST_TYPE_APP, TEST_TYPE_UI) or spm_standalone:
            build_output = {OUTPUT_KEY_TESTS: []}
        else:
            gate_build_start()

            build_cmd = _build_xcodebuild_cmd(
                xcode_config,
                worktree_dir,
                dd_dir,
                clean=False,
            )

            build_result = _run_xcodebuild(
                build_cmd, str(worktree_dir), _build_timeout(xcode_config)
            )

            build_output = parse_build_result(
                build_result.returncode, build_result.stdout, build_result.stderr
            )
            all_stdout = build_result.stdout
            all_stderr = build_result.stderr

            if build_result.returncode != 0:
                save_eval_output(
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

        base_config = (
            _as_ui_test_config(xcode_config)
            if test_type == TEST_TYPE_UI
            else xcode_config
        )
        sim_udid = getattr(_tls, "sim_udid", None)
        test_xcode_config = (
            {
                **base_config,
                "test_destination": f"platform=iOS Simulator,id={sim_udid}",
                "app_test_destination": f"platform=iOS Simulator,id={sim_udid}",
            }
            if sim_udid
            else base_config
        )

        needs_isolated_app_test_dd = run_tests and (
            test_type in (TEST_TYPE_APP, TEST_TYPE_UI) or has_ui_tests
        )
        eval_app_test_dd = (
            cache.prepare_eval_app_test_derived_data(
                repo_name, base_commit, worktree_dir,
                xcode_config=xcode_config,
            )
            if needs_isolated_app_test_dd
            else None
        )

        xctest_output = None
        if run_tests:
            if test_type in (TEST_TYPE_APP, TEST_TYPE_UI):
                xctest_output = _run_app_tests(
                    test_xcode_config,
                    worktree_dir,
                    derived_data_dir=eval_app_test_dd,
                )
            else:
                xctest_output = _run_spm_tests(
                    test_xcode_config,
                    worktree_dir,
                    dd_dir,
                    spm_standalone=bool(spm_standalone),
                )

            if xctest_output:
                all_stdout, all_stderr = _pop_stdout_stderr(
                    xctest_output, all_stdout, all_stderr
                )

            if xctest_output is None and has_task_tests:
                xctest_output = failed_test_result(
                    TEST_NAME_UNIT_TEST_SETUP,
                    "Task tests found but test config not configured in xcode_config.yaml",
                )

            if has_ui_tests:
                ui_config = _as_ui_test_config(test_xcode_config)
                if sim_udid:
                    ui_config = {
                        **ui_config,
                        "app_test_destination": f"platform=iOS Simulator,id={sim_udid}",
                    }
                ui_output = _run_app_tests(
                    ui_config,
                    worktree_dir,
                    derived_data_dir=eval_app_test_dd,
                    is_ui_test=True,
                )
                if ui_output:
                    all_stdout, all_stderr = _pop_stdout_stderr(
                        ui_output, all_stdout, all_stderr
                    )
                    xctest_output = (
                        merge_test_results(xctest_output, ui_output)
                        if xctest_output
                        else ui_output
                    )

        combined = (
            merge_test_results(build_output, xctest_output)
            if xctest_output
            else build_output
        )

        save_eval_output(
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
        result = failed_test_result(
            TEST_NAME_COMPILATION, f"Build timed out ({timeout_s}s)"
        )
        partial_stdout = te.output or ""
        partial_stderr = te.stderr or ""
        save_eval_output(
            output_dir,
            instance_id,
            attempt,
            eval_id,
            result,
            patch,
            partial_stdout,
            partial_stderr,
        )
        return result
    except Exception as e:
        logger.error("Error evaluating %s: %s", tag, e, exc_info=True)
        error_msg = f"{type(e).__name__}: {e}"
        result = failed_test_result(TEST_NAME_EVAL_INFRASTRUCTURE, error_msg[:500])
        try:
            save_eval_output(
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


def run_xcode_evals(
    patches: list[dict],
    instances: list[dict],
    dataset_tasks_dir: Path,
    output_dir: Path,
    eval_id: str,
    max_workers: int | None = None,
    compile_only: bool = False,
    dataset_id: str | None = None,
    run_ui_tests: bool = True,
) -> dict[str, bool]:
    """Run Xcode evals for a batch of patches. Returns instance_id → pass/fail."""
    xcode_config = load_xcode_config(dataset_tasks_dir, dataset_id=dataset_id)
    cache = XcodeBuildCache()

    instance_map = {inst["instance_id"]: inst for inst in instances}

    src_tasks: Path | None = None
    if dataset_id:
        candidate = source_tasks_dir(dataset_id)
        if candidate.is_dir():
            src_tasks = candidate

    if max_workers is None:
        max_workers = DEFAULT_MAX_WORKERS

    eval_results: dict[str, bool] = {}

    _has_tests_cache: dict[str, bool] = {}
    if src_tasks is not None:
        for ps in patches:
            iid = ps["instance_id"]
            if iid not in _has_tests_cache:
                _has_tests_cache[iid] = (
                    src_tasks / TaskTestCopier._task_name(iid) / TESTS_FILENAME
                ).is_file()

    def _has_tests(iid: str) -> bool:
        return _has_tests_cache.get(iid, False)

    real_patches: list[dict] = []
    skipped = 0
    dedup_map: dict[tuple, list[dict]] = {}  # (iid, patch_hash) -> [samples]

    for ps in patches:
        iid = ps["instance_id"]
        patch_text = _get_patch_text(ps)
        if not patch_text or not patch_text.strip():
            attempt = ps.get("attempt")
            result_key = _result_key(iid, attempt)
            eval_results[result_key] = False
            has_tests = _has_tests(iid)
            output = make_empty_patch_result(has_tests)
            save_eval_output(output_dir, iid, attempt, eval_id, output, "", "", "")
            if attempt is not None:
                _write_eval_result_json(output_dir, iid, attempt, False)
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
        f"compile_only={compile_only}, run_ui_tests={run_ui_tests}, "
        f"{n_with_tests} with unit tests)"
    )

    if not real_patches:
        (output_dir / "eval_results.json").write_text(json.dumps(eval_results))
        passed_count = sum(1 for v in eval_results.values() if v)
        typer.echo(f"Xcode eval complete: {passed_count}/{len(eval_results)} passed")
        return eval_results

    needs_tests = n_with_tests > 0 or not compile_only
    test_destination = get_test_destination(xcode_config)
    needs_sim_pool = (
        needs_tests
        and max_workers > 1
        and len(real_patches) > 1
        and test_destination
        and "generic/" not in test_destination
    )

    activate_build_gate()

    passed_count = 0
    eval_durations: list[float] = []
    sim_pool: SimulatorPool | None = None
    try:
        if needs_sim_pool:
            typer.echo(
                f"Creating {actual_workers} simulators for parallel test execution..."
            )
            sim_pool = SimulatorPool(test_destination)
            sim_pool.create(actual_workers)

        def _assign_sim_and_run(patch_sample: dict) -> dict:
            if sim_pool and sim_pool.udids:
                idx = threading.current_thread()._anvil_idx  # type: ignore[attr-defined]
                _tls.sim_udid = sim_pool.udids[idx]
            t0 = time.time()
            result = eval_single_patch(
                patch=_get_patch_text(patch_sample),
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
                run_ui_tests=run_ui_tests,
            )
            eval_durations.append(time.time() - t0)
            return result

        with ThreadPoolExecutor(
            max_workers=actual_workers,
            initializer=make_thread_index_initializer(),
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
                result_key = _result_key(iid, attempt)

                worker_crashed = False
                try:
                    output = future.result()
                except Exception as e:
                    logger.error("Eval failed for %s: %s", result_key, e)
                    output = failed_test_result(
                        TEST_NAME_EVAL_INFRASTRUCTURE,
                        f"Worker error: {type(e).__name__}: {e}"[:500],
                    )
                    worker_crashed = True

                tests = output.get(OUTPUT_KEY_TESTS, [])
                failed = [t for t in tests if t["status"] == TEST_STATUS_FAILED]
                passed_this = len(tests) > 0 and len(failed) == 0
                eval_results[result_key] = passed_this
                if passed_this:
                    passed_count += 1

                patch_text = _get_patch_text(patch_sample)
                dup_key = (iid, hash(patch_text))
                for sibling in dedup_map.get(dup_key, [])[1:]:
                    sib_attempt = sibling.get("attempt")
                    sib_key = _result_key(iid, sib_attempt)
                    eval_results[sib_key] = passed_this
                    if passed_this:
                        passed_count += 1
                    if sib_attempt is not None:
                        _write_eval_result_json(
                            output_dir, iid, sib_attempt, passed_this
                        )

                if attempt is not None:
                    _write_eval_result_json(
                        output_dir, iid, attempt, eval_results[result_key]
                    )
                    if worker_crashed:
                        task_dir = _eval_results_dir(output_dir, iid, attempt)
                        (task_dir / f"{eval_id}_output.json").write_text(
                            json.dumps(output, indent=2)
                        )

                passed = passed_count
                total = len(eval_results)
                status = "pass" if eval_results.get(result_key) else "fail"
                pbar.set_postfix_str(f"{passed}/{total} passed, {result_key} {status}")
    finally:
        deactivate_build_gate()
        if sim_pool:
            typer.echo(f"Cleaning up {len(sim_pool.udids)} eval simulators...")
            sim_pool.destroy()

    (output_dir / "eval_results.json").write_text(json.dumps(eval_results))
    avg_s = sum(eval_durations) / len(eval_durations) if eval_durations else 0
    typer.echo(
        f"Xcode eval complete: {passed_count}/{len(eval_results)} passed"
        f"  |  avg eval time: {avg_s:.0f}s ({avg_s/60:.1f}m)"
    )

    return eval_results
