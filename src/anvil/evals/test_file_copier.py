from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

try:
    from pbxproj import XcodeProject
except ImportError:
    XcodeProject = None

from .constants import (
    PODS_DIR,
    PODS_TARGET_SUPPORT_FILES,
    PODS_XCCONFIG_SUFFIX,
    PROJECT_PBXPROJ,
    TESTS_FILENAME,
    UITESTS_FILENAME,
    TESTABLE_IMPORT_PREFIX,
    TEST_TYPE_APP,
    TEST_TYPE_SPM,
    TEST_TYPE_UI,
    XCODE_CONFIG_APP_TEST_FILES_DEST,
    XCODE_CONFIG_APP_TEST_MODULE,
    XCODE_CONFIG_APP_TEST_SCHEME,
    XCODE_CONFIG_APP_TEST_TARGET,
    XCODE_CONFIG_PROJECT,
    XCODE_CONFIG_SCHEME,
    XCODE_CONFIG_TEST_FILES_DEST,
    XCODE_CONFIG_TEST_PACKAGE_PATH,
    XCODE_CONFIG_TEST_TARGET,
    XCODE_CONFIG_UI_TEST_FILES_DEST,
    XCODE_CONFIG_UI_TEST_TARGET,
)
from .xcode_cache import (
    _pbx_uuid,
    inject_app_test_target,
    inject_ui_test_target,
    resolve_test_package_path,
)

logger = logging.getLogger(__name__)


class TestFileCopier:
    """Copies task test files (tests.swift, uitests.swift) into the worktree."""

    @staticmethod
    def _task_name(instance_id: str) -> str:
        """Extract task name from instance_id (e.g. Repo.task-4 → task-4)."""
        return instance_id.split(".")[-1]

    @staticmethod
    def validate_pbxproj(worktree_dir: Path, project_rel: str) -> str | None:
        """Validate project.pbxproj. Returns error string or None if valid."""
        pbxproj_path = worktree_dir / project_rel / PROJECT_PBXPROJ
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

    @staticmethod
    def _add_file_to_pbxproj(
        worktree_dir: Path,
        project_rel: str,
        file_path: Path,
        target_name: str,
    ) -> None:
        """Add Swift file to pbxproj target's Sources. No-op if already present."""
        pbxproj_path = worktree_dir / project_rel / PROJECT_PBXPROJ
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

    @staticmethod
    def _propagate_pods_framework_paths(
        worktree_dir: Path,
        xcode_config: dict,
        test_target: str,
    ) -> None:
        """Propagate CocoaPods FRAMEWORK_SEARCH_PATHS to test target."""
        scheme = xcode_config.get(XCODE_CONFIG_SCHEME, "")
        project_rel = xcode_config.get(XCODE_CONFIG_PROJECT, "")
        if not scheme or not project_rel:
            return

        pods_xcconfig = (
            worktree_dir
            / PODS_DIR
            / PODS_TARGET_SUPPORT_FILES
            / f"Pods-{scheme}"
            / f"Pods-{scheme}{PODS_XCCONFIG_SUFFIX}"
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

        pbxproj_path = worktree_dir / project_rel / PROJECT_PBXPROJ
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

    def __init__(
        self,
        source_tasks_dir: Path | None,
        xcode_config: dict,
    ):
        """source_tasks_dir: task definitions path. xcode_config: loaded xcode_config.yaml."""
        self.source_tasks_dir = source_tasks_dir
        self.xcode_config = xcode_config

    def copy_task_tests(self, instance_id: str, worktree_dir: Path) -> str:
        """Copy tests.swift to correct target. Returns "ui"/"app"/"spm" or ""."""
        if not self.source_tasks_dir:
            return ""

        tests_file = self.source_tasks_dir / self._task_name(instance_id) / TESTS_FILENAME

        if not tests_file.is_file():
            return ""

        test_type = self._detect_test_type(tests_file, self.xcode_config)

        if test_type == TEST_TYPE_UI:
            return self._copy_to_target(
                instance_id, tests_file, worktree_dir,
                XCODE_CONFIG_UI_TEST_TARGET, XCODE_CONFIG_UI_TEST_FILES_DEST,
                inject_ui_test_target, TEST_TYPE_UI,
            )
        elif test_type == TEST_TYPE_APP:
            return self._copy_to_target(
                instance_id, tests_file, worktree_dir,
                XCODE_CONFIG_APP_TEST_TARGET, XCODE_CONFIG_APP_TEST_FILES_DEST,
                inject_app_test_target, TEST_TYPE_APP,
                fallback_spm=True,
            )
        else:
            return self._copy_spm_tests(instance_id, tests_file, worktree_dir)

    def copy_task_uitests(self, instance_id: str, worktree_dir: Path) -> bool:
        """Copy uitests.swift to UI test target. Returns True if copied."""
        if not self.source_tasks_dir:
            return False

        uitests_file = (
            self.source_tasks_dir / self._task_name(instance_id) / UITESTS_FILENAME
        )

        if not uitests_file.is_file():
            return False

        return self._copy_to_target(
            instance_id, uitests_file, worktree_dir,
            XCODE_CONFIG_UI_TEST_TARGET, XCODE_CONFIG_UI_TEST_FILES_DEST,
            inject_ui_test_target, TEST_TYPE_UI,
        ) == TEST_TYPE_UI

    @staticmethod
    def _detect_test_type(tests_file: Path, xcode_config: dict) -> str:
        """Return "ui", "app", or "spm" from file imports and config."""
        try:
            head = tests_file.read_text()[:500]
        except OSError:
            return TEST_TYPE_SPM

        app_modules = set()
        for key in (XCODE_CONFIG_APP_TEST_MODULE, XCODE_CONFIG_APP_TEST_SCHEME):
            val = xcode_config.get(key, "")
            if val:
                app_modules.add(val)
        if not app_modules:
            return TEST_TYPE_SPM

        for line in head.splitlines()[:10]:
            stripped = line.strip()
            if stripped.startswith(TESTABLE_IMPORT_PREFIX):
                for mod in app_modules:
                    if mod in stripped:
                        return TEST_TYPE_APP
        return TEST_TYPE_SPM

    def _copy_spm_tests(
        self,
        instance_id: str,
        tests_file: Path,
        worktree_dir: Path,
    ) -> str:
        """Copy to SPM test target. Adds to pbxproj and propagates Pod paths if needed."""
        xcode_config = self.xcode_config
        test_files_dest = xcode_config.get(XCODE_CONFIG_TEST_FILES_DEST, "")
        if not test_files_dest:
            return ""

        resolved_pkg = resolve_test_package_path(xcode_config, worktree_dir)
        has_pkg_config = bool(xcode_config.get(XCODE_CONFIG_TEST_PACKAGE_PATH))

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

        test_target = xcode_config.get(XCODE_CONFIG_TEST_TARGET, "")
        project_rel = xcode_config.get(XCODE_CONFIG_PROJECT, "")
        if test_target and not xcode_config.get(XCODE_CONFIG_TEST_PACKAGE_PATH) and project_rel:
            self._add_file_to_pbxproj(worktree_dir, project_rel, dst, test_target)
            self._propagate_pods_framework_paths(worktree_dir, xcode_config, test_target)

        return TEST_TYPE_SPM

    def _copy_to_target(
        self,
        instance_id: str,
        tests_file: Path,
        worktree_dir: Path,
        target_key: str,
        files_dest_key: str,
        inject_fn,
        test_type: str,
        fallback_spm: bool = False,
    ) -> str:
        """Copy test file to configured target. Falls back to SPM if fallback_spm and unconfigured."""
        target = self.xcode_config.get(target_key, "")
        files_dest = self.xcode_config.get(files_dest_key, "")
        if not target or not files_dest:
            if fallback_spm:
                logger.warning(
                    "%s/%s not configured — falling back to spm", target_key, files_dest_key
                )
                return self._copy_spm_tests(instance_id, tests_file, worktree_dir)
            logger.warning(
                "%s/%s not configured for %s", target_key, files_dest_key, instance_id
            )
            return ""

        inject_fn(self.xcode_config, worktree_dir)

        dest_dir = worktree_dir / files_dest
        dest_dir.mkdir(parents=True, exist_ok=True)

        dst = dest_dir / tests_file.name
        shutil.copy2(str(tests_file), str(dst))
        logger.info("Copied test file %s → %s (%s)", tests_file.name, dest_dir, test_type)

        project_rel = self.xcode_config.get(XCODE_CONFIG_PROJECT, "")
        if project_rel:
            self._add_file_to_pbxproj(worktree_dir, project_rel, dst, target)

        return test_type