"""CLI command to pre-warm Xcode build caches for a dataset."""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

import typer
from ruamel.yaml import YAML

from .config import source_tasks_dir, tasks_dir, repo_root
from .evals.xcode_cache import XcodeBuildCache, load_xcode_config


class _RepoCommit(NamedTuple):
    repo_name: str
    base_commit: str


def warm_xcode_cache(
    dataset: str = typer.Option(..., "--dataset", "-d", help="Dataset ID or path"),
    workers: int = typer.Option(2, "--workers", "-w", help="Number of parallel builds"),
) -> None:
    """Pre-build all base commits for a dataset and cache DerivedData."""
    dataset_tasks_dir = tasks_dir(dataset)
    src_tasks_dir = source_tasks_dir(dataset)

    yaml = YAML()
    instances = None
    for candidate in [
        dataset_tasks_dir / "instances.yaml",
        src_tasks_dir / "instances.yaml",
    ]:
        if candidate.exists():
            instances = yaml.load(candidate)
            typer.echo(f"Loaded instances from {candidate}")
            break

    if not instances:
        typer.echo(
            f"Error: instances.yaml not found.\n"
            f"  Searched: {dataset_tasks_dir / 'instances.yaml'}\n"
            f"           {src_tasks_dir / 'instances.yaml'}",
            err=True,
        )
        raise typer.Exit(1)

    try:
        xcode_config = load_xcode_config(dataset_tasks_dir, dataset_id=dataset)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    seen: dict[_RepoCommit, str] = {}
    for inst in instances:
        rc = _RepoCommit(inst["repo_name"], inst["base_commit"])
        if rc not in seen:
            seen[rc] = inst["instance_id"]

    typer.echo(f"Warming Xcode build cache for {dataset}")
    typer.echo(f"  Unique (repo, commit) pairs: {len(seen)}")

    cache = XcodeBuildCache()

    pruned_repos: set[str] = set()
    for rc in seen:
        commit_dir = cache.commit_cache_dir(rc.repo_name, rc.base_commit)
        if not commit_dir.exists():
            continue
        # Skip commits that are already fully cached (main build + all test DDs).
        if cache.is_warm(rc.repo_name, rc.base_commit) and not cache._needs_test_warm(
            xcode_config, rc.repo_name, rc.base_commit
        ):
            continue
        shutil.rmtree(commit_dir)
        typer.echo(f"  Deleted incomplete cache for {rc.repo_name}@{rc.base_commit[:8]}")
        pruned_repos.add(rc.repo_name)

    for repo_name in pruned_repos:
        clone_dir = cache.repo_clone_dir(repo_name)
        if clone_dir.exists():
            subprocess.run(
                ["git", "-C", str(clone_dir), "worktree", "prune"],
                capture_output=True,
            )

    repos_root = repo_root() / "repos"

    unique_repos: dict[str, Path] = {}
    for rc in seen:
        if rc.repo_name not in unique_repos:
            unique_repos[rc.repo_name] = repos_root / rc.repo_name

    valid_repos: set[str] = set()
    for repo_name, repo_path in unique_repos.items():
        if not repo_path.exists():
            typer.echo(
                f"  Skipping {repo_name}: repo not found at {repo_path}", err=True
            )
            continue
        cache.ensure_cloned(repo_name, repo_path)
        valid_repos.add(repo_name)

    def _warm_one(rc: _RepoCommit) -> tuple[_RepoCommit, Exception | None]:
        try:
            cache.warm(repos_root / rc.repo_name, rc.repo_name, rc.base_commit, xcode_config)
            return rc, None
        except Exception as e:
            return rc, e

    valid_commits = [rc for rc in seen if rc.repo_name in valid_repos]
    typer.echo(f"  Building with {workers} parallel worker(s)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_warm_one, rc): rc for rc in valid_commits}
        for future in as_completed(futures):
            rc, error = future.result()
            if error:
                typer.echo(
                    f"  {rc.repo_name}@{rc.base_commit[:8]}: FAILED - {error}", err=True
                )
            else:
                typer.echo(f"  {rc.repo_name}@{rc.base_commit[:8]}: cached")

    typer.echo("Cache warming complete.")
