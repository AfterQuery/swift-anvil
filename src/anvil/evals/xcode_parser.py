"""Parse xcodebuild output into the standardized test result format."""

from __future__ import annotations

import json
import re
from pathlib import Path


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
        status = "PASSED" if match.group(3).lower() == "passed" else "FAILED"
        tests.append({"name": test_name, "class_name": class_name, "status": status})

    swift_testing_pattern = re.compile(
        r"(?:Test|◇|✔|✘)\s+(\w+)\(\)\s+(passed|failed|started)",
        re.IGNORECASE,
    )
    for match in swift_testing_pattern.finditer(combined):
        result = match.group(2).lower()
        if result in ("passed", "failed"):
            tests.append({
                "name": match.group(1),
                "class_name": "",
                "status": "PASSED" if result == "passed" else "FAILED",
            })

    return {"tests": tests}


def parse_build_result(returncode: int, stdout: str, stderr: str) -> dict:
    """Create a synthetic test result for compilation-only checks."""
    combined = stdout + "\n" + stderr

    if returncode == 0:
        return {"tests": [{"name": "compilation", "status": "PASSED"}]}

    error_lines = []
    for line in combined.splitlines():
        if re.search(r"\berror:", line, re.IGNORECASE):
            error_lines.append(line.strip())

    message = "\n".join(error_lines[:5]) if error_lines else "Build failed"
    return {
        "tests": [{"name": "compilation", "status": "FAILED", "message": message}]
    }


def merge_test_results(*results: dict) -> dict:
    """Merge multiple test result dicts into one."""
    all_tests = []
    for r in results:
        if r and "tests" in r:
            all_tests.extend(r["tests"])
    return {"tests": all_tests}
