"""Converters for Anvil evaluation format.

Converts task directories to Anvil's evaluation format which includes:
- instances.yaml - List of instances for run-evals
- gold_patches.json - Reference patches for oracle evaluation
- dockerfiles/ - Docker image definitions

Task directory layout (Xcode):
    metadata.yaml (base_commits for all tasks)
    task-N/task.md, task-N/solution.diff, task-N/tests.swift
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
import yaml

from ..config import repo_root
from ..evals.xcode_cache import load_xcode_config
from ..util import resolve_dataset_path, resolve_registry_env
from ..warm_cache import warm_xcode_cache_for_instances


@dataclass
class Task:
    """A single evaluation task."""

    instance_id: str
    problem_statement: str
    patch: str
    base_commit: str
    repo: str
    before_repo_set_cmd: str = ""


def _load_xcode_task(task_dir: Path, repo_name: str, base_commit: str) -> Task | None:
    """Load a Task from an Xcode-style task directory (task.md + solution.diff)."""
    statement_path = task_dir / "task.md"
    solution_path = task_dir / "solution.diff"

    if not statement_path.exists() or not solution_path.exists():
        return None

    return Task(
        instance_id=f"{repo_name}.{task_dir.name}",
        problem_statement=statement_path.read_text(),
        patch=solution_path.read_text(),
        base_commit=base_commit,
        repo=repo_name,
    )


def _load_base_commits(dataset_path: Path) -> dict[str, str]:
    """Load base commits from the root ``metadata.yaml``.

    Expects a ``base_commits`` mapping of task-name to commit SHA, e.g.::

        base_commits:
          task-1: abc123...
          task-2: def456...

    Returns a dict keyed by task directory name (e.g. ``{"task-1": "abc..."}``)."""
    meta_path = dataset_path / "metadata.yaml"
    if not meta_path.exists():
        return {}
    data = yaml.safe_load(meta_path.read_text()) or {}
    commits = data.get("base_commits", {})
    return {str(k): str(v) for k, v in commits.items()}


def load_all_tasks(dataset_path: Path) -> list[Task]:
    """Load all tasks from an Xcode-format dataset directory.

    Each task-N/ must have task.md and solution.diff.
    Base commits come from the root metadata.yaml.
    """
    task_dirs = sorted(
        [d for d in dataset_path.iterdir() if d.is_dir() and d.name.startswith("task-")],
        key=lambda d: d.name,
    )

    repo_name = dataset_path.name
    base_commits = _load_base_commits(dataset_path)
    tasks = []

    for item in task_dirs:
        base_commit = base_commits.get(item.name, "")

        if not base_commit:
            print(f"Warning: {item.name}: no base_commit in metadata.yaml — skipping", file=sys.stderr)
            continue

        task = _load_xcode_task(item, repo_name, base_commit)
        if task:
            tasks.append(task)

    return tasks


def generate_instances_yaml(
    tasks: list[Task],
    dockerhub_username: str,
    dockerhub_repo: str,
) -> str:
    """Generate instances.yaml content for Anvil's run-evals."""
    instances = []

    for task in tasks:
        repo_name = task.instance_id.partition(".")[0]

        instance: dict = {
            "instance_id": task.instance_id,
            "repo_name": repo_name,
            "base_commit": task.base_commit,
            "problem_statement": task.problem_statement,
        }

        if dockerhub_username:
            instance["image_name"] = f"{dockerhub_username}/{dockerhub_repo}:{task.instance_id}"
        if task.before_repo_set_cmd:
            instance["before_repo_set_cmd"] = task.before_repo_set_cmd

        instances.append(instance)

    return yaml.dump(instances, default_flow_style=False, sort_keys=False)


def generate_gold_patches_json(tasks: list[Task]) -> str:
    """Generate gold_patches.json for oracle evaluation."""
    patches = []

    for task in tasks:
        patch_entry = {
            "instance_id": task.instance_id,
            "patch": task.patch,
            "prefix": "gold",
        }
        patches.append(patch_entry)

    return json.dumps(patches, indent=2)


def convert_to_anvil_structure(
    dataset_path: Path,
    output_path: Path,
    dockerhub_username: str,
    dockerhub_repo: str,
) -> tuple[dict[str, list[Path]], list[Task]]:
    """Convert a task directory to Anvil evaluation format.

    Returns dict of created file paths by category.
    """
    tasks = load_all_tasks(dataset_path)

    if not tasks:
        raise ValueError(f"No tasks found in {dataset_path}")

    project_name = tasks[0].instance_id.partition(".")[0]

    created_files: dict[str, list[Path]] = {
        "config": [],
        "dockerfiles": [],
    }

    output_path.mkdir(parents=True, exist_ok=True)

    dockerfiles_base_dir = (
        output_path / "dockerfiles" / "docker_image_creation" / project_name
    )
    dockerfiles_base_dockerfile_dir = (
        output_path / "dockerfiles" / "base_dockerfile" / project_name
    )
    dockerfiles_instance_dir = output_path / "dockerfiles" / "instance_dockerfile"

    dockerfiles_base_dir.mkdir(parents=True, exist_ok=True)
    dockerfiles_base_dockerfile_dir.mkdir(parents=True, exist_ok=True)
    dockerfiles_instance_dir.mkdir(parents=True, exist_ok=True)

    base_image_tag = f"{dockerhub_username}/{dockerhub_repo}:{project_name}.base"

    # Base Dockerfile from repo
    repo_source = repo_root() / "repos" / project_name
    if repo_source.is_dir():
        remote_url = ""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_source), capture_output=True, text=True,
            )
            if result.returncode == 0:
                remote_url = result.stdout.strip()
        except FileNotFoundError:
            pass

        if remote_url:
            base_dockerfile_content = (
                "FROM ubuntu:24.04\n"
                "RUN apt-get update && apt-get install -y git python3 python3-pip && rm -rf /var/lib/apt/lists/*\n"
                f"RUN git clone {remote_url} /app\n"
                "WORKDIR /app\n"
            )
        else:
            base_dockerfile_content = (
                "FROM ubuntu:24.04\n"
                "RUN apt-get update && apt-get install -y git python3 python3-pip && rm -rf /var/lib/apt/lists/*\n"
                "WORKDIR /app\n"
                "COPY . .\n"
                "RUN git init\n"
            )

        for dest_dir in [dockerfiles_base_dir, dockerfiles_base_dockerfile_dir]:
            dest = dest_dir / "Dockerfile"
            dest.write_text(base_dockerfile_content)
            created_files["dockerfiles"].append(dest)

        if not remote_url:
            shutil.copytree(
                repo_source, dockerfiles_base_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git"),
            )

    # Per-instance Dockerfiles
    for task in tasks:
        instance_docker_dir = dockerfiles_instance_dir / task.instance_id
        instance_docker_dir.mkdir(parents=True, exist_ok=True)

        content = f"FROM {base_image_tag}\nWORKDIR /app\n"
        if task.base_commit:
            content += f"RUN git reset --hard {task.base_commit}\n"

        dest = instance_docker_dir / "Dockerfile"
        dest.write_text(content)
        created_files["dockerfiles"].append(dest)

    # Config files
    instances_yaml = generate_instances_yaml(tasks, dockerhub_username, dockerhub_repo)
    instances_path = output_path / "instances.yaml"
    instances_path.write_text(instances_yaml)
    created_files["config"].append(instances_path)

    gold_patches = generate_gold_patches_json(tasks)
    gold_patches_path = output_path / "gold_patches.json"
    gold_patches_path.write_text(gold_patches)
    created_files["config"].append(gold_patches_path)

    return created_files, tasks


def convert_dataset(
    dataset: Annotated[str, typer.Option("--dataset", "-d", help="Dataset path")],
    dockerhub_username: Annotated[
        str, typer.Option("--dockerhub-username", "-u", help="Docker Hub username (defaults to REGISTRY_USERNAME from .env)")
    ] = "",
    dockerhub_repo: Annotated[
        str, typer.Option("--dockerhub-repo", help="Docker Hub repository name")
    ] = "",
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Output directory")
    ] = None,
) -> None:
    """Convert dataset to Anvil evaluation format.

    Generates instances.yaml, gold_patches.json, and the directory structure
    required for Anvil's publish-images and run-evals commands.
    """
    dockerhub_username, dockerhub_repo = resolve_registry_env(dockerhub_username, dockerhub_repo)
    dataset_path = resolve_dataset_path(dataset)

    if not dataset_path.exists():
        typer.secho(
            f"Error: Dataset directory does not exist: {dataset_path}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    if output_dir:
        output_path = output_dir
    else:
        output_path = repo_root() / "datasets" / dataset_path.name / "tasks"

    typer.echo(f"Converting dataset {dataset_path.name} to Anvil format...")
    typer.echo(f"Output directory: {output_path}")

    try:
        created_files, tasks = convert_to_anvil_structure(
            dataset_path=dataset_path,
            output_path=output_path,
            dockerhub_username=dockerhub_username,
            dockerhub_repo=dockerhub_repo,
        )
    except ValueError as e:
        typer.secho(f"Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.secho("\nConversion completed successfully!", fg=typer.colors.GREEN)

    typer.echo("\nCreated files:")
    typer.echo("  Config:")
    for f in created_files["config"]:
        typer.echo(f"    - {f.relative_to(output_path)}")

    if created_files["dockerfiles"]:
        typer.echo(f"  Dockerfiles: {len(created_files['dockerfiles'])} files")

    dataset_name = dataset_path.name
    ds_path = f"datasets/{dataset_name}"
    has_xcode_config = (dataset_path / "xcode_config.yaml").exists()

    if has_xcode_config:
        try:
            xcode_config = load_xcode_config(dataset_path)
            instances = [
                {"repo_name": t.repo, "base_commit": t.base_commit, "instance_id": t.instance_id}
                for t in tasks
            ]
            warm_xcode_cache_for_instances(
                instances, xcode_config, repo_root() / "repos", dataset_label=dataset_name
            )
        except FileNotFoundError:
            typer.echo("  xcode_config.yaml not found — skipping cache warm.")
        except Exception as e:
            typer.echo(f"  Cache warming failed: {e}", err=True)

    typer.echo("\nNext steps:")
    typer.echo(f"  1. Oracle eval:     anvil run-evals --dataset {ds_path} --agent oracle --compile-only")
    typer.echo(f"  2. Publish images:  anvil publish-images --dataset {ds_path}")
    typer.echo(f"  3. Agent eval:      anvil run-evals --dataset {ds_path} --agent mini-swe-agent --model <model> --compile-only")
