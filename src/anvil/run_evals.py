"""Run evaluations on a dataset."""

from __future__ import annotations

from typing import Annotated

import typer


def run_evals(
    model: str | None = typer.Option(
        None, "--model", help="Model ID (required for agents, optional for oracle)"
    ),
    dataset: str = typer.Option(..., "--dataset", help="Dataset ID or path"),
    agent: Annotated[
        str,
        typer.Option("--agent", help="Agent to use for evaluation (oracle runs golden patches)"),
    ] = "mini-swe-agent",
    n_attempts: Annotated[
        int,
        typer.Option(
            "--n-attempts", "-n", help="Number of attempts per task for pass@k evaluation"
        ),
    ] = 1,
    max_wait: Annotated[
        int | None,
        typer.Option(
            "--max-wait",
            help="Max minutes to wait for Modal rate limits",
        ),
    ] = None,
    max_parallel: Annotated[
        int,
        typer.Option(
            "--max-parallel",
            help="Max concurrent agent runs (Modal sandboxes)",
        ),
    ] = 20,
    no_continue: Annotated[
        bool,
        typer.Option(
            "--no-continue",
            help="Start fresh instead of resuming",
        ),
    ] = False,
    output: str | None = typer.Option(
        None, "--output", help="Output directory override"
    ),
    no_ui_tests: Annotated[
        bool,
        typer.Option(
            "--no-ui-tests",
            help="Run XCTest unit tests only; skip UI tests from uitests.swift. Default: unit tests then UI tests when a task defines them.",
        ),
    ] = False,
    compile_only: Annotated[
        bool,
        typer.Option(
            "--compile-only",
            help="Only check compilation, skip tests",
        ),
    ] = False,
    rollout_only: Annotated[
        bool,
        typer.Option(
            "--rollout-only",
            help="Run agent rollouts only, skip evaluation phase",
        ),
    ] = False,
    task: Annotated[
        list[str] | None,
        typer.Option(
            "--task",
            "-t",
            help="Run only this task (instance_id or short name like 'task-7'). Repeatable.",
        ),
    ] = None,
) -> None:
    """Run evaluation with an agent on a dataset."""
    from .evals import run_evaluation

    rc = run_evaluation(
        model=model,
        dataset_id=dataset,
        agent=agent,
        n_attempts=n_attempts,
        output=output,
        max_wait_minutes=max_wait,
        max_parallel=max_parallel,
        no_continue=no_continue,
        compile_only=compile_only,
        rollout_only=rollout_only,
        task_filter=task,
        run_ui_tests=not no_ui_tests,
    )
    raise typer.Exit(rc)
