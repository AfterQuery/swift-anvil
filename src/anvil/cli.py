"""Anvil CLI - SWE-Bench Pro evaluation toolkit."""

from typing import Sequence

import typer

from . import __version__
from .config import load_repo_env
from .commands import (
    convert_dataset,
    create_tasks,
    publish_images,
    run_evals,
    setup_repo,
    warm_xcode_cache,
)

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
