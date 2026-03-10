import os
import subprocess
from pathlib import Path

import typer

DEFAULT_REGISTRY_REPO = "anvil-images"
DEFAULT_DOCKERHUB_USERNAME = "afterquery"


def run(
    cmd: list[str] | str,
    cwd: Path | None = None,
    env: dict | None = None,
    quiet: bool = False,
) -> int:
    """Run a shell command and return its exit code."""
    if not quiet:
        typer.echo(f"Running: {cmd}")

    return subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    ).returncode


def resolve_dataset_path(dataset: str) -> Path:
    """Resolve a dataset string (relative or absolute) to an absolute Path."""
    p = Path(dataset)
    if not p.is_absolute():
        p = Path.cwd() / dataset
    return p


def resolve_registry_env(
    username: str = "",
    repo: str = "",
) -> tuple[str, str]:
    """Resolve DockerHub username and repo from args or environment."""
    username = username or os.environ.get("REGISTRY_USERNAME", "")
    repo = repo or os.environ.get("REGISTRY_REPO", DEFAULT_REGISTRY_REPO)
    return username, repo


def ensure_dir(path: Path) -> Path:
    """Create directory and parents if needed, return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_id_from_model(model: str) -> str:
    parts = (model or "").split("/")
    if parts and parts[-1]:
        # Replace colons and other problematic chars for filesystem paths
        return parts[-1].replace(":", "_")
    else:
        raise ValueError("Invalid model string")


def provider_env_var_from_model(model: str) -> str:
    provider = (model or "").split("/")[0]
    safe = []
    for ch in provider:
        if ch.isalnum():
            safe.append(ch.upper())
        else:
            safe.append("_")

    name = "".join(safe).strip("_")
    if name == "":
        raise ValueError("Invalid model string")
    return f"${name}_API_KEY"
