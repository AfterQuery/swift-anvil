from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import typer
from ruamel.yaml import YAML as _YAML

from ..config import repo_root, source_tasks_dir

logger = logging.getLogger(__name__)

# Default build timeout when not specified in xcode_config (seconds).
_DEFAULT_BUILD_TIMEOUT = 1200

# Standard Xcode pbxproj constants.
_PBX_UUID_LENGTH = 24
_PBX_BUILD_ACTION_MASK = 2147483647  # INT32_MAX — Xcode default for all build phases


def _apfs_clone(src: Path, dst: Path) -> None:
    """Copy a directory tree using APFS clonefile (instant COW on macOS).

    Falls back to :func:`shutil.copytree` on non-macOS or non-APFS volumes.
    """
    if sys.platform == "darwin":
        result = subprocess.run(
            ["cp", "-c", "-r", "-p", str(src), str(dst)],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        logger.debug("cp -c failed, falling back to shutil.copytree: %s", result.stderr)
    shutil.copytree(str(src), str(dst), symlinks=True)


def _dd_is_populated(path: Path) -> bool:
    """Return True if *path* exists and contains at least one entry."""
    return path.exists() and any(path.iterdir())


def _clone_dd_if_populated(src: Path, dst: Path) -> None:
    """APFS-clone a DerivedData directory if it has content."""
    if _dd_is_populated(src):
        _apfs_clone(src, dst)


def _remove_worktree(clone_dir: Path, work_dir: Path) -> None:
    """Remove a git worktree, falling back to rmtree if git fails."""
    _run_cmd(
        ["git", "-C", str(clone_dir), "worktree", "remove", "--force", str(work_dir)],
        check=False,
    )
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)


def _format_build_errors(
    stderr: str, max_lines: int = 10, fallback_chars: int = 500
) -> str:
    """Extract error lines from xcodebuild stderr, with a fallback tail."""
    error_lines = [ln for ln in stderr.splitlines() if "error:" in ln.lower()]
    if error_lines:
        return "\n".join(error_lines[:max_lines])
    return stderr[-fallback_chars:]


def _default_cache_root() -> Path:
    return repo_root() / ".xcode-cache"


def resolve_test_package_path(xcode_config: dict, work_dir: Path) -> str:
    """Resolve ``test_package_path`` to the first candidate that exists.

    ``test_package_path`` may be a single string or a list of candidate paths.
    Returns the first path where ``Package.swift`` exists under *work_dir*,
    or an empty string if none match.
    """
    raw = xcode_config.get("test_package_path", "")
    if not raw:
        return ""
    candidates = raw if isinstance(raw, list) else [raw]
    for candidate in candidates:
        if (work_dir / candidate / "Package.swift").exists():
            return candidate
    return ""


class XcodeBuildCache:
    """Manages pre-built DerivedData caches per (repo, base_commit) pair."""

    def __init__(self, cache_root: Path | None = None):
        self.cache_root = cache_root or _default_cache_root()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _repo_cache_dir(self, repo_name: str) -> Path:
        return self.cache_root / repo_name

    def commit_cache_dir(self, repo_name: str, base_commit: str) -> Path:
        short = base_commit[:12]
        return self._repo_cache_dir(repo_name) / short

    def repo_clone_dir(self, repo_name: str) -> Path:
        return self._repo_cache_dir(repo_name) / "_repo"

    def _derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / "DerivedData"

    def _test_derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / "DerivedData-tests"

    def _app_test_derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / "DerivedData-app-tests"

    def _main_build_failed_sentinel(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / ".main_build_failed"

    def is_warm(self, repo_name: str, base_commit: str) -> bool:
        # DerivedData populated, or main build was permanently marked as failing
        # (so test DDs can still be cached independently).
        return _dd_is_populated(
            self._derived_data_dir(repo_name, base_commit)
        ) or self._main_build_failed_sentinel(repo_name, base_commit).exists()

    def ensure_cloned(self, repo_name: str, repo_path: Path) -> None:
        """Clone the repo into the cache and fetch all commits.

        Call once per repo before warming commits in parallel to avoid
        concurrent clone/fetch races when multiple commits share a repo.
        """
        clone_dir = self.repo_clone_dir(repo_name)
        if not clone_dir.exists():
            typer.echo(f"  Cloning {repo_name} into cache...")
            _run_cmd(["git", "clone", str(repo_path.resolve()), str(clone_dir)])
        typer.echo(f"  Fetching {repo_name}...")
        _run_cmd(["git", "-C", str(clone_dir), "fetch", "--all"], check=False)

    def warm(
        self,
        repo_path: Path,
        repo_name: str,
        base_commit: str,
        xcode_config: dict,
    ) -> Path:
        """Pre-build a base commit and cache DerivedData.

        Returns the path to the cached DerivedData directory.
        """
        dd_dir = self._derived_data_dir(repo_name, base_commit)
        build_cached = self.is_warm(repo_name, base_commit)

        commit_dir = self.commit_cache_dir(repo_name, base_commit)
        commit_dir.mkdir(parents=True, exist_ok=True)

        clone_dir = self.repo_clone_dir(repo_name)

        # Check if test DDs need warming even when the main build is cached.
        needs_test_warm = self._needs_test_warm(xcode_config, repo_name, base_commit)

        if build_cached and not needs_test_warm:
            logger.info("Cache hit for %s@%s", repo_name, base_commit[:8])
            return dd_dir

        if not clone_dir.exists():
            typer.echo(f"  Cloning {repo_name} into cache...")
            _run_cmd(["git", "clone", str(repo_path.resolve()), str(clone_dir)])
            _run_cmd(["git", "-C", str(clone_dir), "fetch", "--all"], check=False)

        work_dir = commit_dir / "worktree"
        if work_dir.exists():
            _remove_worktree(clone_dir, work_dir)

        typer.echo(f"  Creating worktree at {base_commit[:8]}...")
        _run_cmd(
            [
                "git",
                "-C",
                str(clone_dir),
                "worktree",
                "add",
                "--detach",
                str(work_dir),
                base_commit,
            ]
        )

        sentinel = self._main_build_failed_sentinel(repo_name, base_commit)
        if not build_cached and not sentinel.exists():
            typer.echo(f"  Building {repo_name} (full clean build)...")
            dd_dir.mkdir(parents=True, exist_ok=True)

            build_cmd = _build_xcodebuild_cmd(
                xcode_config,
                work_dir,
                dd_dir,
                clean=True,
                allow_pkg_resolution=True,
            )
            build_timeout = xcode_config.get("build_timeout", _DEFAULT_BUILD_TIMEOUT)
            result = _run_xcodebuild(build_cmd, str(work_dir), build_timeout)

            if result.returncode != 0:
                summary = _format_build_errors(result.stderr)
                shutil.rmtree(dd_dir, ignore_errors=True)
                has_test_schemes = xcode_config.get("test_scheme") or xcode_config.get(
                    "app_test_scheme"
                )
                if has_test_schemes:
                    # Main build failed but test DDs can still be warmed independently.
                    # Write a sentinel so we don't retry this failing build next time.
                    sentinel.touch()
                    typer.echo(
                        f"  Main build failed for {repo_name}@{base_commit[:8]}"
                        f" (will still warm test DDs):\n{summary}",
                        err=True,
                    )
                else:
                    raise RuntimeError(
                        f"xcodebuild failed for {repo_name}@{base_commit[:8]}"
                    )

        self._warm_test_dd(xcode_config, work_dir, repo_name, base_commit)
        self._save_package_resolved(xcode_config, work_dir, repo_name, base_commit)

        _remove_worktree(clone_dir, work_dir)

        typer.echo(f"  Cached DerivedData for {repo_name}@{base_commit[:8]}")
        return dd_dir

    @staticmethod
    def _package_resolved_path(xcode_config: dict, work_dir: Path) -> Path | None:
        """Return the project-level Package.resolved path, or None."""
        project_rel = xcode_config.get("project", "")
        if not project_rel:
            return None
        return (
            work_dir
            / project_rel
            / "project.xcworkspace"
            / "xcshareddata"
            / "swiftpm"
            / "Package.resolved"
        )

    def _save_package_resolved(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Copy Package.resolved from the worktree into the cache."""
        src = self._package_resolved_path(xcode_config, work_dir)
        if not src or not src.exists():
            return
        dst = self.commit_cache_dir(repo_name, base_commit) / "Package.resolved"
        shutil.copy2(src, dst)
        logger.info("Saved Package.resolved for %s@%s", repo_name, base_commit[:8])

    def _restore_package_resolved(
        self,
        xcode_config: dict,
        repo_name: str,
        base_commit: str,
        target_dir: Path,
    ) -> None:
        """Restore cached Package.resolved into a checkout."""
        src = self.commit_cache_dir(repo_name, base_commit) / "Package.resolved"
        if not src.exists():
            return
        dst = self._package_resolved_path(xcode_config, target_dir)
        if not dst:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info("Restored Package.resolved for %s@%s", repo_name, base_commit[:8])

    def _needs_test_warm(
        self, xcode_config: dict, repo_name: str, base_commit: str
    ) -> bool:
        """Check if any test DerivedData directories need warming."""
        if xcode_config.get("test_scheme"):
            if not _dd_is_populated(
                self._test_derived_data_dir(repo_name, base_commit)
            ):
                return True
        if xcode_config.get("app_test_scheme"):
            if not _dd_is_populated(
                self._app_test_derived_data_dir(repo_name, base_commit)
            ):
                return True
        return False

    def _warm_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build test schemes so eval runs skip dependency resolution."""
        self._warm_spm_test_dd(xcode_config, work_dir, repo_name, base_commit)
        self._warm_app_test_dd(xcode_config, work_dir, repo_name, base_commit)

    def _warm_spm_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build the SPM test scheme so eval runs skip dependency resolution."""
        test_scheme = xcode_config.get("test_scheme", "")
        if not test_scheme:
            return

        test_dd_dir = self._test_derived_data_dir(repo_name, base_commit)
        if _dd_is_populated(test_dd_dir):
            return

        resolved_pkg = resolve_test_package_path(xcode_config, work_dir)
        if not resolved_pkg:
            return

        test_files_dest = xcode_config.get("test_files_dest", "")
        if not test_files_dest:
            return

        dummy_dir = work_dir / resolved_pkg / test_files_dest
        dummy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = dummy_dir / "_anvil_warmup.swift"
        dummy_file.write_text("import XCTest\nclass AnvilWarmupTests: XCTestCase {}\n")

        test_dd_dir.mkdir(parents=True, exist_ok=True)
        test_cmd_info = _build_xcodebuild_test_cmd(xcode_config, work_dir, test_dd_dir)
        if not test_cmd_info:
            dummy_file.unlink(missing_ok=True)
            return

        test_cmd, test_cwd = test_cmd_info
        test_cmd = _as_build_for_testing(test_cmd)

        build_timeout = xcode_config.get("build_timeout", _DEFAULT_BUILD_TIMEOUT)
        typer.echo(f"  Warming test DerivedData for {repo_name}@{base_commit[:8]}...")
        result = _run_xcodebuild(test_cmd, str(test_cwd), build_timeout)
        dummy_file.unlink(missing_ok=True)

        if result.returncode != 0:
            summary = _format_build_errors(
                result.stderr, max_lines=5, fallback_chars=300
            )
            typer.echo(f"  Test warm failed (non-fatal): {summary}", err=True)
            shutil.rmtree(test_dd_dir, ignore_errors=True)

    def _warm_app_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build the app-level test scheme (``DerivedData-app-tests``)."""
        app_test_scheme = xcode_config.get("app_test_scheme", "")
        if not app_test_scheme:
            return

        app_test_dd = self._app_test_derived_data_dir(repo_name, base_commit)
        if _dd_is_populated(app_test_dd):
            return

        app_test_target = xcode_config.get("app_test_target", "")
        app_test_files_dest = xcode_config.get("app_test_files_dest", "")
        if not app_test_target or not app_test_files_dest:
            return

        inject_app_test_target(xcode_config, work_dir)

        dummy_dir = work_dir / app_test_files_dest
        dummy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = dummy_dir / "_anvil_warmup.swift"
        dummy_file.write_text(
            "import XCTest\nclass AnvilAppWarmupTests: XCTestCase {\n"
            "    func testWarmup() { XCTAssertTrue(true) }\n}\n"
        )

        app_test_dd.mkdir(parents=True, exist_ok=True)
        cmd_info = _build_xcodebuild_app_test_cmd(
            xcode_config,
            work_dir,
            app_test_dd,
            allow_pkg_resolution=True,
        )
        if not cmd_info:
            dummy_file.unlink(missing_ok=True)
            return

        cmd, cwd = cmd_info
        cmd = _as_build_for_testing(cmd)

        build_timeout = xcode_config.get("build_timeout", _DEFAULT_BUILD_TIMEOUT)
        typer.echo(
            f"  Warming app-test DerivedData for {repo_name}@{base_commit[:8]}..."
        )
        result = _run_xcodebuild(cmd, str(cwd), build_timeout)
        dummy_file.unlink(missing_ok=True)

        if result.returncode != 0:
            summary = _format_build_errors(
                result.stderr, max_lines=5, fallback_chars=300
            )
            typer.echo(f"  App-test warm failed (non-fatal): {summary}", err=True)
            shutil.rmtree(app_test_dd, ignore_errors=True)

    def checkout(
        self,
        repo_name: str,
        base_commit: str,
        target_dir: Path,
        xcode_config: dict | None = None,
    ) -> Path:
        """Create an isolated worktree with pre-built DerivedData.

        Returns target_dir (the worktree root).
        """
        clone_dir = self.repo_clone_dir(repo_name)
        if not clone_dir.exists():
            raise RuntimeError(
                f"No cached repo for {repo_name}. Run 'anvil warm-xcode-cache' first."
            )

        if target_dir.exists():
            self.cleanup(repo_name, target_dir)

        _run_cmd(
            [
                "git",
                "-C",
                str(clone_dir),
                "worktree",
                "add",
                "--detach",
                str(target_dir),
                base_commit,
            ]
        )

        _clone_dd_if_populated(
            self._derived_data_dir(repo_name, base_commit),
            target_dir / "DerivedData",
        )
        _clone_dd_if_populated(
            self._test_derived_data_dir(repo_name, base_commit),
            target_dir / "DerivedData-tests",
        )
        _clone_dd_if_populated(
            self._app_test_derived_data_dir(repo_name, base_commit),
            target_dir / "DerivedData-app-tests",
        )

        #  Removing the module cache forces Xcode to rebuild it cheaply while still reusing compiled products.
        for dd_name in ("DerivedData", "DerivedData-tests", "DerivedData-app-tests"):
            module_cache = target_dir / dd_name / "ModuleCache.noindex"
            if module_cache.exists():
                shutil.rmtree(module_cache, ignore_errors=True)

        if xcode_config:
            self._restore_package_resolved(
                xcode_config, repo_name, base_commit, target_dir
            )

        return target_dir

    def cleanup(self, repo_name: str, target_dir: Path) -> None:
        """Remove a worktree created by checkout()."""
        clone_dir = self.repo_clone_dir(repo_name)
        if clone_dir.exists():
            _run_cmd(
                [
                    "git",
                    "-C",
                    str(clone_dir),
                    "worktree",
                    "remove",
                    "--force",
                    str(target_dir),
                ],
                check=False,
            )
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)


# Shared xcodebuild flags used across build/test commands.
_XCODEBUILD_NO_SIGN_FLAGS = [
    "-skipPackagePluginValidation",
    "ONLY_ACTIVE_ARCH=YES",
    "CODE_SIGNING_ALLOWED=NO",
    "CODE_SIGN_IDENTITY=",
    "COMPILER_INDEX_STORE_ENABLE=NO",
]
# App-hosted tests need ad-hoc signing so entitlements (e.g. CloudKit) are preserved.
_XCODEBUILD_ADHOC_SIGN_FLAGS = [
    "-skipPackagePluginValidation",
    "ONLY_ACTIVE_ARCH=YES",
    "CODE_SIGNING_ALLOWED=YES",
    "CODE_SIGN_IDENTITY=-",
    "COMPILER_INDEX_STORE_ENABLE=NO",
]


def _as_build_for_testing(cmd: list[str]) -> list[str]:
    """Replace the 'test' action with 'build-for-testing' in an xcodebuild command."""
    return ["build-for-testing" if c == "test" else c for c in cmd]


def _resolve_project_args(xcode_config: dict, work_dir: Path) -> list[str]:
    """Resolve -workspace/-project args, preferring workspace when it exists."""
    workspace = xcode_config.get("workspace", "")
    project = xcode_config.get("project", "")

    workspace_path = work_dir / workspace if workspace else None
    project_path = work_dir / project if project else None

    if workspace_path and workspace_path.exists():
        return ["-workspace", str(workspace_path)]
    elif project_path and project_path.exists():
        return ["-project", str(project_path)]
    elif workspace:
        return ["-workspace", str(workspace_path)]
    elif project:
        return ["-project", str(project_path)]
    return []


def _build_xcodebuild_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    clean: bool = False,
    allow_pkg_resolution: bool = False,
) -> list[str]:
    """Build the xcodebuild compile command from config."""
    scheme = xcode_config["scheme"]
    destination = xcode_config.get(
        "destination",
        "generic/platform=iOS Simulator",
    )

    cmd = ["xcodebuild"]
    if clean:
        cmd.append("clean")
    cmd.append("build")

    cmd.extend(_resolve_project_args(xcode_config, work_dir))
    cmd.extend(
        [
            "-scheme",
            scheme,
            "-destination",
            destination,
            "-derivedDataPath",
            str(derived_data_dir),
            "-quiet",
            *_XCODEBUILD_NO_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    cmd.extend(xcode_config.get("extra_build_flags", []))

    return cmd


def _build_xcodebuild_test_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    test_only: list[str] | None = None,
    allow_pkg_resolution: bool = False,
) -> tuple[list[str], Path] | None:
    """Build the xcodebuild test command.

    Returns (cmd, cwd) or None if no test config.

    When ``test_package_path`` is set in the config, the test command targets
    the standalone SPM package (no -project/-workspace flags) and ``cwd`` is
    set to the package directory.  Otherwise the main project is used and
    ``cwd`` is the worktree root.

    Args:
        test_only: Optional list of test identifiers to run.
        allow_pkg_resolution: If True, omit ``-disableAutomaticPackageResolution``
            (used during cache warming).
    """
    test_scheme = xcode_config.get("test_scheme", "")
    if not test_scheme:
        return None

    test_destination = xcode_config.get(
        "test_destination",
        xcode_config.get("destination", ""),
    )
    if not test_destination or "generic/" in test_destination:
        return None

    test_package_path = resolve_test_package_path(xcode_config, work_dir)

    cmd = ["xcodebuild", "test"]

    if test_package_path:
        cwd = work_dir / test_package_path
    elif xcode_config.get("test_package_path"):
        logger.warning(
            "No test_package_path candidate has Package.swift at %s — skipping tests",
            work_dir,
        )
        return None
    else:
        cwd = work_dir
        cmd.extend(_resolve_project_args(xcode_config, work_dir))

    cmd.extend(
        [
            "-scheme",
            test_scheme,
            "-destination",
            test_destination,
            "-derivedDataPath",
            str(derived_data_dir),
            *_XCODEBUILD_NO_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    cmd.extend(xcode_config.get("extra_build_flags", []))

    only = test_only or xcode_config.get("test_only", [])
    for target in only:
        cmd.extend(["-only-testing:" + target])

    return cmd, cwd


def inject_app_test_target(xcode_config: dict, work_dir: Path) -> bool:
    """Programmatically inject a unit-test target into the Xcode project.

    Creates the ``ACHNBrowserUITests`` target (or whatever ``app_test_target``
    names) in the project.pbxproj and wires it into the scheme's TestAction.
    Also creates the ``Info.plist`` on disk.

    This replaces a static patch approach because the pbxproj context lines
    differ across base commits, but section markers and key UUIDs are stable.

    Returns True if injection succeeded, False if skipped or failed.
    """
    app_test_target = xcode_config.get("app_test_target", "")
    app_test_files_dest = xcode_config.get("app_test_files_dest", "")
    project_rel = xcode_config.get("project", "")
    if not app_test_target or not app_test_files_dest or not project_rel:
        return False

    pbxproj_path = work_dir / project_rel / "project.pbxproj"
    if not pbxproj_path.exists():
        logger.warning("project.pbxproj not found at %s", pbxproj_path)
        return False

    pbx = pbxproj_path.read_text()
    if app_test_target in pbx:
        logger.debug("Target %s already exists, skipping injection", app_test_target)
        return True

    def _uuid(seed: str) -> str:
        return hashlib.md5(seed.encode()).hexdigest().upper()[:_PBX_UUID_LENGTH]

    uid = {
        k: _uuid(f"{app_test_target}-{k}")
        for k in [
            "group",
            "info_plist_ref",
            "placeholder_ref",
            "placeholder_build",
            "product_ref",
            "sources_phase",
            "resources_phase",
            "frameworks_phase",
            "target",
            "config_debug",
            "config_release",
            "config_list",
            "target_dep",
            "container_proxy",
        ]
    }

    # Discover the host app target UUID and project object UUID from pbxproj
    m = re.search(
        r"(\w{24}) /\* "
        + re.escape(xcode_config.get("scheme", ""))
        + r" \*/ = \{\s*isa = PBXNativeTarget;",
        pbx,
    )
    if not m:
        logger.warning("Could not find host app target in pbxproj")
        return False
    host_target_uuid = m.group(1)

    m = re.search(r"rootObject = (\w{24})", pbx)
    if not m:
        return False
    project_uuid = m.group(1)

    # Find the Products group UUID
    m = re.search(r"productRefGroup = (\w{24})", pbx)
    products_group_uuid = m.group(1) if m else None

    # Find the main group UUID
    m = re.search(r"mainGroup = (\w{24})", pbx)
    main_group_uuid = m.group(1) if m else None

    # Discover the app's PRODUCT_NAME for TEST_HOST
    m = re.search(r"productReference = \w{24} /\* (.+?)\.app \*/", pbx)
    app_product_name = m.group(1) if m else xcode_config.get("scheme", "")

    # 1. PBXBuildFile
    pbx = pbx.replace(
        "/* End PBXBuildFile section */",
        f"\t\t{uid['placeholder_build']} /* {app_test_target}Placeholder.swift in Sources */  = "
        f"{{isa = PBXBuildFile; fileRef = {uid['placeholder_ref']} /* {app_test_target}Placeholder.swift */; }};\n"
        "/* End PBXBuildFile section */",
    )

    # 2. PBXContainerItemProxy
    proxy_block = (
        f"\t\t{uid['container_proxy']} /* PBXContainerItemProxy */ = {{\n"
        f"\t\t\tisa = PBXContainerItemProxy;\n"
        f"\t\t\tcontainerPortal = {project_uuid} /* Project object */;\n"
        f"\t\t\tproxyType = 1;\n"
        f"\t\t\tremoteGlobalIDString = {host_target_uuid};\n"
        f"\t\t\tremoteInfo = {xcode_config.get('scheme', '')};\n"
        f"\t\t}};\n"
    )
    if "/* End PBXContainerItemProxy section */" in pbx:
        pbx = pbx.replace(
            "/* End PBXContainerItemProxy section */",
            proxy_block + "/* End PBXContainerItemProxy section */",
        )
    else:
        pbx = pbx.replace(
            "/* Begin PBXCopyFilesBuildPhase section */",
            "/* Begin PBXContainerItemProxy section */\n"
            + proxy_block
            + "/* End PBXContainerItemProxy section */\n\n"
            "/* Begin PBXCopyFilesBuildPhase section */",
        )

    # 3. PBXFileReference
    pbx = pbx.replace(
        "/* End PBXFileReference section */",
        f"\t\t{uid['info_plist_ref']} /* Info.plist */ = "
        f'{{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>"; }};\n'
        f"\t\t{uid['placeholder_ref']} /* {app_test_target}Placeholder.swift */ = "
        f'{{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {app_test_target}Placeholder.swift; sourceTree = "<group>"; }};\n'
        f"\t\t{uid['product_ref']} /* {app_test_target}.xctest */ = "
        f"{{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = {app_test_target}.xctest; sourceTree = BUILT_PRODUCTS_DIR; }};\n"
        "/* End PBXFileReference section */",
    )

    # 4. PBXFrameworksBuildPhase
    pbx = pbx.replace(
        "/* End PBXFrameworksBuildPhase section */",
        f"\t\t{uid['frameworks_phase']} /* Frameworks */ = {{\n"
        f"\t\t\tisa = PBXFrameworksBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXFrameworksBuildPhase section */",
    )

    # 5. PBXGroup for the test target
    # Group path is relative to the project dir, so use just the target name
    group_rel_path = app_test_target
    pbx = pbx.replace(
        "/* End PBXGroup section */",
        f"\t\t{uid['group']} /* {app_test_target} */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{uid['info_plist_ref']} /* Info.plist */,\n"
        f"\t\t\t\t{uid['placeholder_ref']} /* {app_test_target}Placeholder.swift */,\n"
        f"\t\t\t);\n"
        f"\t\t\tpath = {group_rel_path};\n"
        f'\t\t\tsourceTree = "<group>";\n\t\t}};\n'
        "/* End PBXGroup section */",
    )

    # Add group to main group and product to Products group
    if main_group_uuid and products_group_uuid:
        pbx = pbx.replace(
            f"{products_group_uuid} /* Products */,",
            f"{uid['group']} /* {app_test_target} */,\n\t\t\t\t{products_group_uuid} /* Products */,",
            1,
        )
        # Add product ref to Products group children (before closing paren)
        products_children_end = re.search(
            rf"{products_group_uuid} /\* Products \*/ = \{{\s*isa = PBXGroup;\s*children = \((.*?)\);",
            pbx,
            re.DOTALL,
        )
        if products_children_end:
            insert_pos = products_children_end.end(1)
            pbx = (
                pbx[:insert_pos]
                + f"\n\t\t\t\t{uid['product_ref']} /* {app_test_target}.xctest */,"
                + pbx[insert_pos:]
            )

    # 6. PBXNativeTarget
    pbx = pbx.replace(
        "/* End PBXNativeTarget section */",
        f"\t\t{uid['target']} /* {app_test_target} */ = {{\n"
        f"\t\t\tisa = PBXNativeTarget;\n"
        f"\t\t\tbuildConfigurationList = {uid['config_list']} /* Build configuration list for PBXNativeTarget \"{app_test_target}\" */;\n"
        f"\t\t\tbuildPhases = (\n"
        f"\t\t\t\t{uid['sources_phase']} /* Sources */,\n"
        f"\t\t\t\t{uid['frameworks_phase']} /* Frameworks */,\n"
        f"\t\t\t\t{uid['resources_phase']} /* Resources */,\n"
        f"\t\t\t);\n"
        f"\t\t\tbuildRules = (\n\t\t\t);\n"
        f"\t\t\tdependencies = (\n"
        f"\t\t\t\t{uid['target_dep']} /* PBXTargetDependency */,\n"
        f"\t\t\t);\n"
        f"\t\t\tname = {app_test_target};\n"
        f"\t\t\tproductName = {app_test_target};\n"
        f"\t\t\tproductReference = {uid['product_ref']} /* {app_test_target}.xctest */;\n"
        f'\t\t\tproductType = "com.apple.product-type.bundle.unit-test";\n'
        f"\t\t}};\n"
        "/* End PBXNativeTarget section */",
    )

    # 7. Add to project targets list
    pbx = re.sub(
        rf"(targets = \([^)]*{re.escape(host_target_uuid)}[^)]*)\);",
        rf'\1\t\t\t\t{uid["target"]} /* {app_test_target} */,\n\t\t\t);',
        pbx,
        count=1,
    )

    # 8. TargetAttributes
    m = re.search(r"(TargetAttributes = \{.*?)((\s*\};){2})", pbx, re.DOTALL)
    if m:
        insert_at = m.start(2)
        attr_block = (
            f"\n\t\t\t\t\t{uid['target']} = {{\n"
            f"\t\t\t\t\t\tCreatedOnToolsVersion = 12.0;\n"
            f"\t\t\t\t\t\tTestTargetID = {host_target_uuid};\n"
            f"\t\t\t\t\t}};"
        )
        pbx = pbx[:insert_at] + attr_block + pbx[insert_at:]

    # 9. PBXResourcesBuildPhase
    pbx = pbx.replace(
        "/* End PBXResourcesBuildPhase section */",
        f"\t\t{uid['resources_phase']} /* Resources */ = {{\n"
        f"\t\t\tisa = PBXResourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXResourcesBuildPhase section */",
    )

    # 10. PBXSourcesBuildPhase
    pbx = pbx.replace(
        "/* End PBXSourcesBuildPhase section */",
        f"\t\t{uid['sources_phase']} /* Sources */ = {{\n"
        f"\t\t\tisa = PBXSourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n"
        f"\t\t\t\t{uid['placeholder_build']} /* {app_test_target}Placeholder.swift in Sources */,\n"
        f"\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXSourcesBuildPhase section */",
    )

    # 11. PBXTargetDependency
    dep_block = (
        f"\t\t{uid['target_dep']} /* PBXTargetDependency */ = {{\n"
        f"\t\t\tisa = PBXTargetDependency;\n"
        f"\t\t\ttarget = {host_target_uuid} /* {xcode_config.get('scheme', '')} */;\n"
        f"\t\t\ttargetProxy = {uid['container_proxy']} /* PBXContainerItemProxy */;\n"
        f"\t\t}};\n"
    )
    if "/* End PBXTargetDependency section */" in pbx:
        pbx = pbx.replace(
            "/* End PBXTargetDependency section */",
            dep_block + "/* End PBXTargetDependency section */",
        )
    else:
        pbx = pbx.replace(
            "/* Begin XCBuildConfiguration section */",
            "/* Begin PBXTargetDependency section */\n"
            + dep_block
            + "/* End PBXTargetDependency section */\n\n"
            "/* Begin XCBuildConfiguration section */",
        )

    # 12. XCBuildConfiguration (Debug + Release)
    iphoneos_target = xcode_config.get("iphoneos_deployment_target", "14.0")
    bundle_id = xcode_config.get("app_test_bundle_id", "com.anvil.tests")

    dev_team = xcode_config.get("development_team", "")
    if not dev_team:
        m = re.search(r"DEVELOPMENT_TEAM\s*=\s*(\w+)", pbx)
        dev_team = m.group(1) if m else ""

    dev_team_line = f"\t\t\t\tDEVELOPMENT_TEAM = {dev_team};\n" if dev_team else ""

    for cfg_uuid, cfg_name in [
        (uid["config_debug"], "Debug"),
        (uid["config_release"], "Release"),
    ]:
        pbx = pbx.replace(
            "/* End XCBuildConfiguration section */",
            f"\t\t{cfg_uuid} /* {cfg_name} */ = {{\n"
            f"\t\t\tisa = XCBuildConfiguration;\n"
            f"\t\t\tbuildSettings = {{\n"
            f'\t\t\t\tBUNDLE_LOADER = "$(TEST_HOST)";\n'
            f"\t\t\t\tCODE_SIGN_STYLE = Automatic;\n"
            f"{dev_team_line}"
            f"\t\t\t\tINFOPLIST_FILE = {app_test_target}/Info.plist;\n"
            f"\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = {iphoneos_target};\n"
            f"\t\t\t\tLD_RUNPATH_SEARCH_PATHS = (\n"
            f'\t\t\t\t\t"$(inherited)",\n'
            f'\t\t\t\t\t"@executable_path/Frameworks",\n'
            f'\t\t\t\t\t"@loader_path/Frameworks",\n'
            f"\t\t\t\t);\n"
            f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {bundle_id};\n"
            f'\t\t\t\tPRODUCT_NAME = "$(TARGET_NAME)";\n'
            f"\t\t\t\tSWIFT_VERSION = 5.0;\n"
            f'\t\t\t\tTARGETED_DEVICE_FAMILY = "1,2";\n'
            f'\t\t\t\tTEST_HOST = "$(BUILT_PRODUCTS_DIR)/{app_product_name}.app/{app_product_name}";\n'
            f"\t\t\t}};\n"
            f"\t\t\tname = {cfg_name};\n\t\t}};\n"
            "/* End XCBuildConfiguration section */",
        )

    # 13. XCConfigurationList
    pbx = pbx.replace(
        "/* End XCConfigurationList section */",
        f"\t\t{uid['config_list']} /* Build configuration list for PBXNativeTarget \"{app_test_target}\" */ = {{\n"
        f"\t\t\tisa = XCConfigurationList;\n"
        f"\t\t\tbuildConfigurations = (\n"
        f"\t\t\t\t{uid['config_debug']} /* Debug */,\n"
        f"\t\t\t\t{uid['config_release']} /* Release */,\n"
        f"\t\t\t);\n"
        f"\t\t\tdefaultConfigurationIsVisible = 0;\n"
        f"\t\t\tdefaultConfigurationName = Release;\n\t\t}};\n"
        "/* End XCConfigurationList section */",
    )

    pbxproj_path.write_text(pbx)

    # Update scheme to include test target in TestAction
    scheme_dir = work_dir / project_rel / "xcshareddata" / "xcschemes"
    scheme_name = (
        xcode_config.get("app_test_scheme", xcode_config.get("scheme", ""))
        + ".xcscheme"
    )
    scheme_path = scheme_dir / scheme_name
    if scheme_path.exists():
        scheme_xml = scheme_path.read_text()
        if app_test_target not in scheme_xml:
            testable_entry = (
                f"         <TestableReference\n"
                f'            skipped = "NO">\n'
                f"            <BuildableReference\n"
                f'               BuildableIdentifier = "primary"\n'
                f"               BlueprintIdentifier = \"{uid['target']}\"\n"
                f'               BuildableName = "{app_test_target}.xctest"\n'
                f'               BlueprintName = "{app_test_target}"\n'
                f"               ReferencedContainer = \"container:{project_rel.split('/')[-1]}\">\n"
                f"            </BuildableReference>\n"
                f"         </TestableReference>\n"
            )
            if "      <Testables>\n      </Testables>" in scheme_xml:
                scheme_xml = scheme_xml.replace(
                    "      <Testables>\n      </Testables>",
                    f"      <Testables>\n{testable_entry}      </Testables>",
                )
            elif "      </Testables>" in scheme_xml:
                scheme_xml = scheme_xml.replace(
                    "      </Testables>",
                    f"{testable_entry}      </Testables>",
                )
            scheme_path.write_text(scheme_xml)

    # Create Info.plist and placeholder test file on disk
    test_dir = work_dir / app_test_files_dest
    test_dir.mkdir(parents=True, exist_ok=True)

    info_plist_path = test_dir / "Info.plist"
    if not info_plist_path.exists():
        info_plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "\t<key>CFBundleDevelopmentRegion</key>\n\t<string>$(DEVELOPMENT_LANGUAGE)</string>\n"
            "\t<key>CFBundleExecutable</key>\n\t<string>$(EXECUTABLE_NAME)</string>\n"
            "\t<key>CFBundleIdentifier</key>\n\t<string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>\n"
            "\t<key>CFBundleInfoDictionaryVersion</key>\n\t<string>6.0</string>\n"
            "\t<key>CFBundleName</key>\n\t<string>$(PRODUCT_NAME)</string>\n"
            "\t<key>CFBundlePackageType</key>\n\t<string>$(PRODUCT_BUNDLE_PACKAGE_TYPE)</string>\n"
            "\t<key>CFBundleShortVersionString</key>\n\t<string>1.0</string>\n"
            "\t<key>CFBundleVersion</key>\n\t<string>1</string>\n"
            "</dict>\n</plist>\n"
        )

    placeholder_path = test_dir / f"{app_test_target}Placeholder.swift"
    if not placeholder_path.exists():
        # Detect the actual Swift module name from the pbxproj's app product reference
        # Falls back to app_test_module from config if detection fails.
        module_name = xcode_config.get(
            "app_test_module", xcode_config.get("scheme", "")
        )
        # Match the app product used by the host target, avoiding TV/watch extensions.
        host_target_scheme = xcode_config.get(
            "app_test_scheme", xcode_config.get("scheme", "")
        )
        host_m = re.search(
            rf"productName\s*=\s*{re.escape(host_target_scheme)};.*?"
            r"productReference\s*=\s*\w+ /\*\s*([^*]+?\.app)\s*\*/",
            pbx,
            re.DOTALL,
        )
        if host_m:
            product_name = host_m.group(1).strip()[:-4]  # strip .app
            detected = product_name.replace(" ", "_").replace("-", "_")
            if detected:
                module_name = detected
        placeholder_path.write_text(
            f"import XCTest\n@testable import {module_name}\n\n"
            f"final class {app_test_target}Placeholder: XCTestCase {{\n"
            f"    func testPlaceholder() {{ XCTAssertTrue(true) }}\n}}\n"
        )

    logger.info("Injected %s target into %s", app_test_target, pbxproj_path)
    return True


def _build_xcodebuild_app_test_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    allow_pkg_resolution: bool = False,
) -> tuple[list[str], Path] | None:
    """Build the xcodebuild test command for **app-level** unit tests.

    Returns ``(cmd, cwd)`` or ``None`` when no app test config is present.

    Unlike :func:`_build_xcodebuild_test_cmd` (SPM package tests), this
    targets the main Xcode project using ``app_test_scheme`` and runs tests
    hosted inside the app bundle.  Uses ad-hoc signing (``CODE_SIGN_IDENTITY=-``)
    so that entitlements (e.g. CloudKit container) are preserved on simulator.
    """
    app_test_scheme = xcode_config.get("app_test_scheme", "")
    if not app_test_scheme:
        return None

    dest = xcode_config.get(
        "app_test_destination",
        xcode_config.get("test_destination", xcode_config.get("destination", "")),
    )
    if not dest or "generic/" in dest:
        return None

    cmd = ["xcodebuild", "test"]
    cmd.extend(_resolve_project_args(xcode_config, work_dir))
    cmd.extend(
        [
            "-scheme",
            app_test_scheme,
            "-destination",
            dest,
            "-derivedDataPath",
            str(derived_data_dir),
            *_XCODEBUILD_ADHOC_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    cmd.extend(xcode_config.get("extra_build_flags", []))

    return cmd, work_dir


def _run_cmd(
    cmd: list[str], check: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        **kwargs,
    )


def _run_xcodebuild(
    cmd: list[str],
    cwd: str,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Run xcodebuild, killing the entire process group on timeout."""
    logger.debug("Running xcodebuild: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def load_xcode_config(dataset_tasks_dir: Path, dataset_id: str | None = None) -> dict:
    """Load xcode_config.yaml, searching generated and source task dirs."""
    candidates = [dataset_tasks_dir / "xcode_config.yaml"]

    if dataset_id:
        candidates.append(source_tasks_dir(dataset_id) / "xcode_config.yaml")

    for path in candidates:
        if path.exists():
            return _YAML().load(path)

    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"No xcode_config.yaml found. Searched: {searched}. "
        "Create one with project/scheme/destination settings."
    )
