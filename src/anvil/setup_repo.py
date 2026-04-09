"""CLI command to set up a new repository for task creation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer


def setup_repo(
    repo_url: str = typer.Argument(..., help="GitHub repository URL (e.g., https://github.com/user/repo)"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing directories without prompting"),
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

    (task_path / "repo.md").write_text(f"""\
# {repo_name}

Repository: {repo_url}

## Tasks

1. [Task Name](link/to/pr)

- Type: Feature
- Patch: curl -L [pr-link].diff -o solution.diff
- Patch Commit: [commit-hash]
- Base Commit: [base-commit-hash]

## Commands

```bash
source .venv/bin/activate
```

0. Clone source repo

```bash
git clone {repo_url} repos/{repo_name}
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
""")

    (task_path / "metadata.yaml").write_text("""\
base_commits:
  task-1: [base-commit-hash]
""")

    (task_path / "xcode_config.yaml").write_text(f"""\
# Xcode build configuration for {repo_name}
# Used by: anvil warm-xcode-cache --dataset datasets/{repo_name}
#           anvil run-evals --eval-backend xcode --dataset datasets/{repo_name}

# project: {repo_name}/{repo_name}.xcodeproj
# scheme: {repo_name}

# Per-task unit tests.
# Each task can have a single tests.swift file at tasks/{repo_name}/task-N/tests.swift.
# test_package_path:
#   - {repo_name}/Packages/Backend
# test_files_dest: Tests/BackendTests
# test_scheme: Backend
# test_destination: "platform=iOS Simulator,name=iPhone 17 Pro,OS=latest"

# App-level unit tests
# app_test_scheme: {repo_name}
# app_test_target: {repo_name}Tests
# app_test_files_dest: {repo_name}/{repo_name}Tests
# app_test_module: {repo_name}

# UI tests
# ui_test_target: {repo_name}UITests
# ui_test_files_dest: {repo_name}/{repo_name}UITests

# build_timeout: 600
""")

    task1_path = task_path / "task-1"
    task1_path.mkdir()

    (task1_path / "task.md").write_text("""\
## Feature: [Task Title]

### Problem Description

[Describe the problem the user faces and why it matters.]

### Acceptance Criteria

1. [Criterion 1]
2. [Criterion 2]

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `[TypeName.methodName()]`
""")

    (task1_path / "tests.swift").write_text(f"""\
import XCTest
@testable import {repo_name}

final class AnvilTask1Tests: XCTestCase {{

    func testPlaceholder() throws {{
        // TODO: implement tests
        XCTFail("Not implemented")
    }}

}}
""")

    (task1_path / "uitests.swift").write_text("""\
import XCTest

final class AnvilTask1UITests: XCTestCase {

    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    func testPlaceholder() throws {
        // TODO: implement UI tests
        XCTFail("Not implemented")
    }

}
""")

    typer.echo(f"\n✓ Repository cloned to: {repos_path}")
    typer.echo(f"✓ Task structure created at: {task_path}")
    typer.echo("\nNext steps:")
    typer.echo(f"  1. Edit {task_path}/repo.md to add task details")
    typer.echo(f"  2. Edit {task_path}/metadata.yaml to add base commits")
    typer.echo(f"  3. Edit {task_path}/xcode_config.yaml with Xcode configuration")
    typer.echo(f"  4. Fill in {task1_path}/task.md, tests.swift, and uitests.swift")
