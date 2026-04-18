"""Anvil CLI - SWE-Bench Pro evaluation toolkit."""

from typing import Sequence

import typer

from . import __version__
from .config import load_repo_env
from .publish import publish_images
from .run_evals import run_evals
from .setup_repo import setup_repo
from .warm_cache import warm_xcode_cache
from .wizard.converters import convert_dataset
from .wizard.task_creator import create_tasks

load_repo_env()

app = typer.Typer(help="AQ Project Anvil - SWE-Bench Pro Tasks", no_args_is_help=True)
app.command("setup-repo", no_args_is_help=True)(setup_repo)
app.command("create-tasks", no_args_is_help=True)(create_tasks)
app.command("convert-dataset", no_args_is_help=True)(convert_dataset)
app.command("warm-xcode-cache", no_args_is_help=True)(warm_xcode_cache)
app.command("publish-images", no_args_is_help=True)(publish_images)
app.command("run-evals", no_args_is_help=True)(run_evals)


def version_callback(v: bool) -> None:
    if v:
        typer.echo(f"v{__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    _version: bool = typer.Option(
        False, "-v", "--version", is_eager=True, callback=version_callback
    )
) -> None:
    pass


def main(argv: Sequence[str] | None = None) -> int:
    return app(
        args=list(argv) if argv is not None else None,
        standalone_mode=False,
    )
