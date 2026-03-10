"""Run evaluations on a dataset."""

from __future__ import annotations

from typing import Annotated, Literal

import typer

from .util import resolve_registry_env


def run_evals(
    model: str | None = typer.Option(
        None, "--model", help="Model ID (required for agents, optional for oracle)"
    ),
    dataset: str = typer.Option(..., "--dataset", help="Dataset ID or path"),
    agent: Annotated[
        Literal["mini-swe-agent", "oracle"],
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
            help="Max concurrent runs",
        ),
    ] = 30,
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
    dockerhub_username: str = typer.Option(
        "", "--dockerhub-username", "-u", help="DockerHub username (defaults to REGISTRY_USERNAME from .env)"
    ),
    dockerhub_repo: str = typer.Option(
        "", "--dockerhub-repo", help="DockerHub repo name"
    ),
    eval_backend: Annotated[
        Literal["xcode", "modal"],
        typer.Option(
            "--eval-backend",
            help="Evaluation backend: 'xcode' (local macOS xcodebuild) or 'modal' (Linux Docker via Modal)",
        ),
    ] = "xcode",
    compile_only: Annotated[
        bool,
        typer.Option(
            "--compile-only",
            help="(xcode backend) Only check compilation, skip tests",
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
    dockerhub_username, dockerhub_repo = resolve_registry_env(dockerhub_username, dockerhub_repo)
    if eval_backend == "modal" and not dockerhub_username:
        typer.echo("Docker Hub username required for modal backend. Set REGISTRY_USERNAME in .env or pass -u.", err=True)
        raise typer.Exit(1)

    from .evals import run_evaluation

    rc = run_evaluation(
        model=model,
        dataset_id=dataset,
        dockerhub_username=dockerhub_username,
        dockerhub_repo=dockerhub_repo,
        agent=agent,
        n_attempts=n_attempts,
        output=output,
        max_wait_minutes=max_wait,
        max_parallel=max_parallel,
        no_continue=no_continue,
        eval_backend=eval_backend,
        compile_only=compile_only,
        rollout_only=rollout_only,
        task_filter=task,
    )
    raise typer.Exit(rc)
