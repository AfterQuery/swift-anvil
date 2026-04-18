"""CLI command to set up a new repository for task creation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from .llm import DEFAULT_MODEL, call_llm, llm_available
from .prompts import XCODE_CONFIG_SYSTEM

XCODE_CONFIG_PLACEHOLDER = """\
# Xcode build configuration for {repo_name}
# Used by: anvil warm-xcode-cache --dataset datasets/{repo_name}
#           anvil run-evals --eval-backend xcode --dataset datasets/{repo_name}
#
# IMPORTANT: All paths are relative to the repo root (the git worktree root).
# Do NOT add a repo-name prefix unless the .xcodeproj actually lives in a
# subdirectory. Check the repo structure to determine the correct paths.

# If the repo uses CocoaPods, set both workspace and project:
# workspace: {repo_name}.xcworkspace
# project: {repo_name}.xcodeproj
# scheme: {repo_name}

# Shell commands to run before xcodebuild (e.g. generate stubs, pod install):
# pre_build_commands:
#   - "if [ -f Podfile ]; then pod install --no-repo-update; fi"

# Per-task unit tests.
# Each task can have a single tests.swift file at tasks/{repo_name}/task-N/tests.swift.
# test_package_path:
#   - Packages/Backend
# test_files_dest: Tests/BackendTests
# test_scheme: Backend
# test_destination: "platform=iOS Simulator,name=iPhone 17 Pro,OS=latest"

# App-level unit tests
# app_test_scheme: {repo_name}
# app_test_target: {repo_name}Tests
# app_test_files_dest: {repo_name}Tests
# app_test_module: {repo_name}

# UI tests
# ui_test_target: {repo_name}UITests
# ui_test_files_dest: {repo_name}UITests

# build_timeout: 600
"""


def _get_repo_tree(repos_path: Path, max_depth: int = 3) -> str:
    """Get a directory listing of the cloned repo for the LLM."""
    result = subprocess.run(
        ["find", str(repos_path), "-maxdepth", str(max_depth), "-type", "f"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    # Make paths relative to repos_path
    prefix = str(repos_path) + "/"
    lines = [
        line.removeprefix(prefix)
        for line in result.stdout.strip().splitlines()
        if not line.endswith(".DS_Store")
    ]
    return "\n".join(sorted(lines)[:500])


def _generate_xcode_config(repo_name: str, repo_tree: str) -> str | None:
    """Use LLM to generate xcode_config.yaml from the repo structure."""
    if not llm_available():
        return None
    user_msg = f"Repository name: {repo_name}\n\nDirectory listing:\n{repo_tree}"
    return call_llm(DEFAULT_MODEL, XCODE_CONFIG_SYSTEM, user_msg)


def setup_repo(
    repo_url: str = typer.Argument(
        ..., help="GitHub repository URL (e.g., https://github.com/user/repo)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing directories without prompting"
    ),
) -> None:
    """Clone a repository and scaffold the task directory structure under tasks/."""
    repo_name = repo_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    typer.echo(f"Setting up repository: {repo_name}")
    typer.echo(f"URL: {repo_url}")

    repos_dir = Path("repos")
    tasks_dir = Path("tasks")
    task_path = tasks_dir / repo_name
    repos_path = repos_dir / repo_name

    repos_dir.mkdir(exist_ok=True)
    tasks_dir.mkdir(exist_ok=True)

    # Clone the repo
    if repos_path.exists():
        if force:
            typer.echo(f"Removing existing repository at {repos_path}")
            shutil.rmtree(repos_path)
        else:
            if not typer.confirm(f"Repository exists at {repos_path}. Overwrite?"):
                typer.echo("Skipping repository clone")
                return
            shutil.rmtree(repos_path)

    typer.echo("Cloning repository...")
    result = subprocess.run(
        ["git", "clone", "--progress", repo_url, str(repos_path)],
    )
    if result.returncode != 0:
        typer.echo("Error: git clone failed", err=True)
        raise typer.Exit(1)

    # Create task directory
    if task_path.exists():
        if force:
            typer.echo(f"Removing existing task structure at {task_path}")
            shutil.rmtree(task_path)
        else:
            if not typer.confirm(f"Task directory exists at {task_path}. Overwrite?"):
                typer.echo("Skipping task structure creation")
                return
            shutil.rmtree(task_path)

    typer.echo(f"Creating task structure in {task_path}...")
    task_path.mkdir(parents=True, exist_ok=True)

    (task_path / "repo.md").write_text(
        f"""\
# {repo_name}

Repository: {repo_url}

## Tasks

1. [Task Name](link/to/pr)

- Type: Feature
- Patch: curl -L [pr-link].diff -o solution.diff
- Base Commit: [base-commit-hash]

## Commands

```bash
source .venv/bin/activate
```

0. Create task directories from GitHub PRs (skip if task dirs already exist)

Add PR URLs (one per line) to `src/anvil/commands/github_prs/{repo_name}.txt`, then run:

```bash
anvil create-tasks {repo_name}
```

1. Write tasks and convert dataset

```bash
anvil convert-dataset --dataset tasks/{repo_name}
```

2. Verify gold patches

```bash
anvil run-evals --dataset datasets/{repo_name} --agent oracle --no-continue
```

3. Publish Docker images

```bash
anvil publish-images --dataset datasets/{repo_name}
```

4. Run against models

```bash
anvil run-evals --dataset datasets/{repo_name} --agent mini-swe-agent --model openrouter/anthropic/claude-opus-4.6 --n-attempts 4 --no-continue
```
"""
    )

    # Generate xcode_config.yaml via LLM, fall back to placeholder
    typer.echo("Generating xcode_config.yaml...")
    xcode_config: str | None = None
    try:
        repo_tree = _get_repo_tree(repos_path)
        if repo_tree:
            xcode_config = _generate_xcode_config(repo_name, repo_tree)
    except Exception as e:
        typer.secho(
            f"Warning: LLM xcode_config generation failed: {e}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not xcode_config:
        xcode_config = XCODE_CONFIG_PLACEHOLDER.format(repo_name=repo_name)
        if llm_available():
            typer.secho(
                "Falling back to placeholder xcode_config.yaml.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    (task_path / "xcode_config.yaml").write_text(xcode_config)

    typer.echo(f"\n✓ Repository cloned to: {repos_path}")
    typer.echo(f"✓ Task structure created at: {task_path}")
    typer.echo("\nNext steps:")
    typer.echo(f"  1. Add PR URLs to src/anvil/commands/github_prs/{repo_name}.txt")
    typer.echo(f"  2. Run: anvil create-tasks {repo_name}")
    typer.echo(f"  3. Review {task_path}/xcode_config.yaml")
