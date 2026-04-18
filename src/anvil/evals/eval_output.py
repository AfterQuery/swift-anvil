from __future__ import annotations

import json
from pathlib import Path

from .constants import (
    OUTPUT_KEY_TESTS,
    TEST_NAME_PATCH_CONTENT,
    TEST_STATUS_FAILED,
)


def save_eval_output(
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


def failed_test_result(name: str, message: str) -> dict:
    """Return a synthetic FAILED test result dict."""
    return {
        OUTPUT_KEY_TESTS: [
            {"name": name, "status": TEST_STATUS_FAILED, "message": message}
        ]
    }


def make_empty_patch_result(has_tests: bool) -> dict:
    """Return a synthetic FAILED result for an empty/blank patch."""
    msg = (
        "Empty patch — skipped build (tests would fail on unpatched base)"
        if has_tests
        else "Empty patch — nothing to evaluate"
    )
    return failed_test_result(TEST_NAME_PATCH_CONTENT, msg)
