from __future__ import annotations

# -----------------------------------------------------------------------------
# Simulator
# -----------------------------------------------------------------------------
SIMULATOR_NAME_PREFIX = "anvil-eval"
DEFAULT_DEVICE_NAME = "iPhone 16"

# -----------------------------------------------------------------------------
# Test file names
# -----------------------------------------------------------------------------
TESTS_FILENAME = "tests.swift"
UITESTS_FILENAME = "uitests.swift"

# -----------------------------------------------------------------------------
# Xcode project paths
# -----------------------------------------------------------------------------
PROJECT_PBXPROJ = "project.pbxproj"
PODS_DIR = "Pods"
PODS_TARGET_SUPPORT_FILES = "Target Support Files"
PODS_XCCONFIG_SUFFIX = ".debug.xcconfig"

# -----------------------------------------------------------------------------
# Xcode config keys (xcode_config.yaml schema)
# -----------------------------------------------------------------------------
XCODE_CONFIG_SCHEME = "scheme"
XCODE_CONFIG_PROJECT = "project"
XCODE_CONFIG_TEST_FILES_DEST = "test_files_dest"
XCODE_CONFIG_TEST_TARGET = "test_target"
XCODE_CONFIG_TEST_PACKAGE_PATH = "test_package_path"
XCODE_CONFIG_APP_TEST_TARGET = "app_test_target"
XCODE_CONFIG_APP_TEST_FILES_DEST = "app_test_files_dest"
XCODE_CONFIG_APP_TEST_MODULE = "app_test_module"
XCODE_CONFIG_APP_TEST_SCHEME = "app_test_scheme"
XCODE_CONFIG_UI_TEST_TARGET = "ui_test_target"
XCODE_CONFIG_UI_TEST_FILES_DEST = "ui_test_files_dest"

# -----------------------------------------------------------------------------
# Test type detection (file content markers)
# -----------------------------------------------------------------------------
XCUI_APPLICATION_IMPORT = "XCUIApplication"
TESTABLE_IMPORT_PREFIX = "@testable import"

# -----------------------------------------------------------------------------
# Test types (TestFileCopier.copy_task_tests return values)
# -----------------------------------------------------------------------------
TEST_TYPE_APP = "app"
TEST_TYPE_SPM = "spm"
TEST_TYPE_UI = "ui"

# -----------------------------------------------------------------------------
# Test status (our output format)
# -----------------------------------------------------------------------------
TEST_STATUS_PASSED = "PASSED"
TEST_STATUS_FAILED = "FAILED"

# Raw xcodebuild output strings (lowercase, from Test Case lines)
XCODEBUILD_PASSED = "passed"
XCODEBUILD_FAILED = "failed"

# -----------------------------------------------------------------------------
# Output keys
# -----------------------------------------------------------------------------
OUTPUT_KEY_TESTS = "tests"

# -----------------------------------------------------------------------------
# Eval / build settings
# -----------------------------------------------------------------------------
DEFAULT_XCODEBUILD_TIMEOUT = 600
DEFAULT_BUILD_TIMEOUT = 1200  # Cache warming / main build
DEFAULT_MAX_WORKERS = 3
BUILD_GATE_SECONDS = 1

# UI test config keys → app test config keys (for _as_ui_test_config mapping)
UI_TO_APP_CONFIG_KEYS = (
    ("ui_test_scheme", "app_test_scheme"),
    ("ui_test_target", "app_test_target"),
    ("ui_test_files_dest", "app_test_files_dest"),
    ("ui_test_destination", "app_test_destination"),
    ("ui_test_bundle_id", "app_test_bundle_id"),
)

# -----------------------------------------------------------------------------
# Synthetic / infrastructure test names
# -----------------------------------------------------------------------------
TEST_NAME_COMPILATION = "compilation"
TEST_NAME_UNIT_TEST_SETUP = "unit_test_setup"
TEST_NAME_PATCH_APPLY = "patch_apply"
TEST_NAME_XCTEST_RUN = "xctest_run"
TEST_NAME_EVAL_INFRASTRUCTURE = "eval_infrastructure"
TEST_NAME_PBXPROJ_VALIDATION = "pbxproj_validation"
TEST_NAME_PATCH_CONTENT = "patch_content"

SYNTHETIC_TEST_NAMES = frozenset(
    {
        TEST_NAME_COMPILATION,
        TEST_NAME_XCTEST_RUN,
        TEST_NAME_UNIT_TEST_SETUP,
        TEST_NAME_PATCH_APPLY,
    }
)
