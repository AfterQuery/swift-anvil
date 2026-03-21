import tempfile
from pathlib import Path

from anvil.evals.constants import (
    TEST_TYPE_APP,
    TEST_TYPE_SPM,
    TEST_TYPE_UI,
    XCODE_CONFIG_APP_TEST_MODULE,
    XCODE_CONFIG_UI_TEST_FILES_DEST,
    XCODE_CONFIG_UI_TEST_TARGET,
)
from anvil.evals.test_file_copier import TaskTestCopier


def _write_swift(tmp: Path, content: str) -> Path:
    p = tmp / "tests.swift"
    p.write_text(content)
    return p


def test_detect_ui_type():
    with tempfile.TemporaryDirectory() as d:
        f = _write_swift(Path(d), "import XCTest\nlet _ = XCUIApplication()\n")
        config = {
            XCODE_CONFIG_UI_TEST_TARGET: "MyUITests",
            XCODE_CONFIG_UI_TEST_FILES_DEST: "MyUITests/",
        }
        assert TaskTestCopier._detect_test_type(f, config) == TEST_TYPE_UI


def test_detect_ui_requires_target_and_dest():
    with tempfile.TemporaryDirectory() as d:
        # Has XCUIApplication but missing ui_test_files_dest → not UI
        f = _write_swift(Path(d), "import XCTest\nlet _ = XCUIApplication()\n")
        config = {XCODE_CONFIG_UI_TEST_TARGET: "MyUITests"}
        assert TaskTestCopier._detect_test_type(f, config) != TEST_TYPE_UI


def test_detect_app_type():
    with tempfile.TemporaryDirectory() as d:
        f = _write_swift(Path(d), "@testable import MyApp\nimport XCTest\n")
        config = {XCODE_CONFIG_APP_TEST_MODULE: "MyApp"}
        assert TaskTestCopier._detect_test_type(f, config) == TEST_TYPE_APP


def test_detect_spm_fallback():
    with tempfile.TemporaryDirectory() as d:
        f = _write_swift(Path(d), "import XCTest\n")
        assert TaskTestCopier._detect_test_type(f, {}) == TEST_TYPE_SPM


def test_detect_spm_when_no_matching_module():
    with tempfile.TemporaryDirectory() as d:
        f = _write_swift(Path(d), "@testable import OtherApp\nimport XCTest\n")
        config = {XCODE_CONFIG_APP_TEST_MODULE: "MyApp"}
        assert TaskTestCopier._detect_test_type(f, config) == TEST_TYPE_SPM


def test_task_name_dotted():
    assert TaskTestCopier._task_name("ACHNBrowserUI.task-4") == "task-4"


def test_task_name_plain():
    assert TaskTestCopier._task_name("task-1") == "task-1"
