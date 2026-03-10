"""CLI command for validating task tests against the unpatched base commit."""

from __future__ import annotations

from typing import Annotated

import typer


def validate_tests(
    dataset: Annotated[
        str,
        typer.Option("--dataset", "-d", help="Dataset ID or path (e.g. datasets/ACHNBrowserUI)"),
    ],
    max_workers: Annotated[
        int | None,
        typer.Option("--workers", "-w", help="Max parallel xcodebuild processes (default: 2)"),
    ] = None,
) -> None:
    """Run task tests on the unpatched base commit and check consistency.

    Tests are categorized by class name:
      - Classes containing "F2P" (e.g. AnvilTask1F2PTests) — fail-to-pass; must fail on base.
      - All other classes (repo tests, Anvil*P2P*, etc.) — pass-to-pass; must pass on base.

    Reports inconsistencies (f2p tests that pass, or p2p tests that fail).

    To verify gold patches make all tests pass, use: anvil run-evals --agent oracle
    """
    from .evals.xcode_eval import validate_task_tests

    rc = validate_task_tests(dataset_id=dataset, max_workers=max_workers)
    raise typer.Exit(rc)
