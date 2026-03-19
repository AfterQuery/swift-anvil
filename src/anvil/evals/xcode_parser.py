"""Parse xcodebuild output into the standardized test result format."""

from __future__ import annotations

import re

from .constants import (
    OUTPUT_KEY_TESTS,
    TEST_NAME_COMPILATION,
    TEST_STATUS_FAILED,
    TEST_STATUS_PASSED,
    XCODEBUILD_FAILED,
    XCODEBUILD_PASSED,
)


def parse_xcodebuild_output(stdout: str, stderr: str) -> dict:
    """Parse xcodebuild stdout/stderr into ``{tests: [{name, class_name, status}]}`` format.

    Handles:
    - Build-only results (synthetic compilation test)
    - xcodebuild test verbose output (Test Case lines)
    """
    tests = []
    combined = stdout + "\n" + stderr

    test_case_pattern = re.compile(
        r"Test Case\s+['\"]?-\[(\S+)\s+(\w+)\]['\"]?\s+(passed|failed)",
        re.IGNORECASE,
    )
    for match in test_case_pattern.finditer(combined):
        class_name = match.group(1)
        test_name = match.group(2)
        status = TEST_STATUS_PASSED if match.group(3).lower() == XCODEBUILD_PASSED else TEST_STATUS_FAILED
        tests.append({"name": test_name, "class_name": class_name, "status": status})

    swift_testing_pattern = re.compile(
        r"(?:Test|◇|✔|✘)\s+(\w+)\(\)\s+(passed|failed|started)",
        re.IGNORECASE,
    )
    for match in swift_testing_pattern.finditer(combined):
        result = match.group(2).lower()
        if result in (XCODEBUILD_PASSED, XCODEBUILD_FAILED):
            tests.append({
                "name": match.group(1),
                "class_name": "",
                "status": TEST_STATUS_PASSED if result == XCODEBUILD_PASSED else TEST_STATUS_FAILED,
            })

    return {OUTPUT_KEY_TESTS: tests}


def parse_build_result(returncode: int, stdout: str, stderr: str) -> dict:
    """Create a synthetic test result for compilation-only checks."""
    combined = stdout + "\n" + stderr

    if returncode == 0:
        return {OUTPUT_KEY_TESTS: [{"name": TEST_NAME_COMPILATION, "status": TEST_STATUS_PASSED}]}

    error_lines = []
    for line in combined.splitlines():
        if re.search(r"\berror:", line, re.IGNORECASE):
            error_lines.append(line.strip())

    message = "\n".join(error_lines[:5]) if error_lines else "Build failed"
    return {
        OUTPUT_KEY_TESTS: [{"name": TEST_NAME_COMPILATION, "status": TEST_STATUS_FAILED, "message": message}]
    }


def merge_test_results(*results: dict) -> dict:
    """Merge multiple test result dicts into one."""
    all_tests = []
    for r in results:
        if r and OUTPUT_KEY_TESTS in r:
            all_tests.extend(r[OUTPUT_KEY_TESTS])
    return {OUTPUT_KEY_TESTS: all_tests}
