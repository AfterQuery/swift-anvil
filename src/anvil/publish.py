"""Publish dataset images to Docker Hub."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import typer
from ruamel.yaml import YAML

from .util import resolve_registry_env

_RETRYABLE_PUSH_ERRORS = ("broken pipe", "connection reset", "timeout", "EOF", "TLS handshake")


@dataclass
class BuildTask:
    """A Docker image to build and push."""

    name: str
    dockerfile: Path
    context: Path

    def tag(self, username: str, repo: str) -> str:
        return f"{username}/{repo}:{self.name}"


def _docker_logged_in() -> bool:
    """Check if Docker CLI has stored credentials."""
    cfg = Path.home() / ".docker" / "config.json"
    if not cfg.exists():
        return False
    try:
        return bool(json.loads(cfg.read_text()).get("auths"))
    except Exception:
        return False


def _is_public_repo(username: str, repo: str) -> bool:
    """Check if Docker Hub repo is publicly visible. Returns False if private or unknown."""
    url = f"https://hub.docker.com/v2/repositories/{username}/{repo}/"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return not json.loads(resp.read()).get("is_private", False)
    except Exception:
        return False  # Private repos return 404, unknown is treated as safe


def _discover_build_tasks(tasks_dir: Path) -> tuple[list[BuildTask], list[BuildTask]]:
    """Find Docker images to build.

    Returns (base_tasks, instance_tasks) - base images must be built first.

    For base images, if a local clone exists at ``repos/<project>/``, it is
    used as the Docker build context instead of the (usually empty) creation
    directory.  This avoids ``git clone`` from GitHub during the build.
    """
    creation_dir = tasks_dir / "dockerfiles" / "docker_image_creation"
    instance_df_dir = tasks_dir / "dockerfiles" / "instance_dockerfile"

    if not creation_dir.exists():
        return [], []

    contexts = {d.name: d for d in creation_dir.iterdir() if d.is_dir()}
    repos_dir = Path("repos")

    # Base images: built from docker_image_creation/<project>/Dockerfile
    base_tasks = []
    for name, context in sorted(contexts.items()):
        dockerfile = context / "Dockerfile"
        if dockerfile.exists():
            local_repo = repos_dir / name
            if (local_repo / ".git").is_dir():
                context = local_repo
            base_tasks.append(BuildTask(name=f"{name}.base", dockerfile=dockerfile, context=context))

    # Instance images: built from instance_dockerfile/<project>.<task>/Dockerfile
    instance_tasks = []
    if instance_df_dir.exists():
        for task_dir in sorted(instance_df_dir.iterdir()):
            dockerfile = task_dir / "Dockerfile"
            if not task_dir.is_dir() or not dockerfile.exists():
                continue
            project = task_dir.name.partition(".")[0]
            context = contexts.get(project)
            if context:
                local_repo = repos_dir / project
                if (local_repo / ".git").is_dir():
                    context = local_repo
                instance_tasks.append(BuildTask(name=task_dir.name, dockerfile=dockerfile, context=context))

    return base_tasks, instance_tasks


def _patch_dockerfile_if_needed(dockerfile: Path, username: str, repo: str, context: Path | None = None) -> str:
    """Return Dockerfile content with COPY . . inserted after FROM if missing.

    When *context* points to a local repo clone, ``RUN git clone …`` lines are
    replaced with ``COPY . <dest>`` so the build uses the local checkout
    instead of fetching from GitHub.
    """
    content = dockerfile.read_text()

    # Rewrite FROM to use user's repo
    content = re.sub(r"^(FROM\s+)\S+/\S+:", rf"\1{username}/{repo}:", content, count=1, flags=re.MULTILINE)

    # Replace `RUN git clone <url> <dest>` with COPY from local context.
    if context and (context / ".git").is_dir():
        content = re.sub(
            r"^RUN\s+git\s+clone\s+\S+\s+(/\S+)\s*$",
            r"COPY . \1",
            content,
            flags=re.MULTILINE,
        )

    if re.search(r"(?:COPY|ADD)\s+\.\s", content):
        return content

    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("FROM "):
            # Skip comments/blanks after FROM
            insert_at = i + 1
            while insert_at < len(lines) and (
                not lines[insert_at].strip() or lines[insert_at].strip().startswith("#")
            ):
                insert_at += 1
            lines.insert(insert_at, "WORKDIR /app")
            lines.insert(insert_at + 1, "COPY . .")
            return "\n".join(lines)

    return content


def _push_with_retry(tag: str, max_retries: int = 3) -> tuple[bool, str]:
    """Push a Docker image with retries for transient network errors."""
    for attempt in range(1, max_retries + 1):
        result = subprocess.run(["docker", "push", tag], capture_output=True, text=True)
        if result.returncode == 0:
            return True, ""
        full_err = (result.stderr or result.stdout or "").strip()
        retryable = any(s in full_err for s in _RETRYABLE_PUSH_ERRORS)
        if not retryable or attempt == max_retries:
            return False, full_err
        wait = 5 * attempt
        print(f"  Push attempt {attempt}/{max_retries} failed (retrying in {wait}s): {full_err.splitlines()[-1]}")
        time.sleep(wait)
    return False, "max retries exceeded"


def _build_and_push(task: BuildTask, username: str, repo: str, platform: str) -> tuple[str | None, str | None]:
    """Build and push a Docker image. Returns (tag, None) on success, (None, error) on failure."""
    tag = task.tag(username, repo)
    patched_content = _patch_dockerfile_if_needed(task.dockerfile, username, repo, context=task.context)

    build_cmd = [
        "docker", "build",
        "--platform", platform,
        "--provenance=false",
        "-f", "-",
        "-t", tag,
        str(task.context),
    ]
    result = subprocess.run(build_cmd, input=patched_content, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_lines = result.stderr.strip().split("\n")
        err_detail = stderr_lines[-1] if stderr_lines else "build failed"
        return None, f"[build] {err_detail}"

    ok, err = _push_with_retry(tag)
    if not ok:
        print(f"  Push failed for {tag}:\n{err}")
        err_detail = err.split("\n")[-1] if err else "push failed"
        return None, f"[push] {err_detail}"

    return tag, None


def _update_instances_yaml(
    inst_path: Path, built: dict[str, str], username: str, repo: str
) -> int:
    """Update instances.yaml with new image names. Returns count updated."""
    yaml = YAML()
    yaml.preserve_quotes = True

    with inst_path.open() as f:
        instances = yaml.load(f)

    updated = 0
    for inst in instances:
        iid = inst.get("instance_id", "")
        project = iid.partition(".")[0]
        # Use built tag if available, otherwise construct from instance_id
        tag = built.get(iid) or built.get(project)
        if not tag:
            # Always update to new repo even if build failed
            tag = f"{username}/{repo}:{iid}"
        inst["image_name"] = tag
        updated += 1

    with inst_path.open("w") as f:
        yaml.dump(instances, f)

    return updated


def publish_images(
    dataset_id: str = typer.Option(..., "--dataset", help="Dataset ID"),
    dockerhub_username: str = typer.Option("", "--dockerhub-username", "-u", help="Docker Hub username (defaults to REGISTRY_USERNAME from .env)"),
    platform: str = typer.Option("linux/amd64", "--platform", help="Docker platform"),
    repo_name: str = typer.Option("", "--repo", help="Docker Hub repository name"),
    max_workers: int = typer.Option(4, "--max-workers", "-j", help="Max parallel builds (lower to avoid rate limits)"),
) -> None:
    """Build and push dataset images to your private Docker Hub."""
    dockerhub_username, repo_name = resolve_registry_env(dockerhub_username, repo_name)
    if not dockerhub_username:
        typer.echo("Docker Hub username required. Set REGISTRY_USERNAME in .env or pass -u.", err=True)
        raise typer.Exit(1)

    tasks_dir = Path(dataset_id) / "tasks"

    if not _docker_logged_in():
        typer.echo("Not logged into Docker. Run `docker login` first.", err=True)
        raise typer.Exit(1)

    if _is_public_repo(dockerhub_username, repo_name):
        typer.echo(f"Repository {dockerhub_username}/{repo_name} is PUBLIC. Refusing to push.", err=True)
        raise typer.Exit(1)

    base_tasks, instance_tasks = _discover_build_tasks(tasks_dir)
    all_tasks = base_tasks + instance_tasks
    if not all_tasks:
        typer.echo(f"No Dockerfiles found in {tasks_dir}/dockerfiles/", err=True)
        raise typer.Exit(1)

    typer.echo(f"Building {len(all_tasks)} image(s) ({len(base_tasks)} base + {len(instance_tasks)} instance)...")

    built: dict[str, str] = {}
    failed: list[str] = []
    counter = [0]  # mutable for closure

    def run_builds(tasks: list[BuildTask]) -> None:
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=min(len(tasks), max_workers)) as executor:
            futures = {
                executor.submit(_build_and_push, task, dockerhub_username, repo_name, platform): task
                for task in tasks
            }
            for future in as_completed(futures):
                counter[0] += 1
                task = futures[future]
                try:
                    tag, err = future.result()
                    if tag:
                        typer.echo(f"[{counter[0]}/{len(all_tasks)}] {task.name} ✓")
                        built[task.name] = tag
                    else:
                        typer.echo(f"[{counter[0]}/{len(all_tasks)}] {task.name} ✗ {err}", err=True)
                        failed.append(task.name)
                except Exception as e:
                    typer.echo(f"[{counter[0]}/{len(all_tasks)}] {task.name} ✗ {e}", err=True)
                    failed.append(task.name)

    run_builds(base_tasks)
    run_builds(instance_tasks)

    if not built:
        typer.echo("All builds failed", err=True)
        raise typer.Exit(1)

    inst_path = tasks_dir / "instances.yaml"
    if inst_path.exists():
        updated = _update_instances_yaml(inst_path, built, dockerhub_username, repo_name)
        typer.echo(f"Updated {updated} instance(s) in instances.yaml")
    else:
        typer.echo(f"{inst_path} not found, skipping update", err=True)

    if failed:
        typer.echo(f"Failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)
