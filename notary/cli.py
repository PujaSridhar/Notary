"""Notary command line (SPEC section 11, item 8)."""

from __future__ import annotations

import json

import typer

from . import db as dbmod

app = typer.Typer(help="Notary: a test gate for AI agent memory.", no_args_is_help=True)


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def check() -> None:
    """Ping the Atlas Sandbox and confirm transactions work."""
    _print(dbmod.check())


@app.command()
def seed(seed: int = 7) -> None:
    """Generate customers, orders and tasks into the Sandbox (replaces existing ones)."""
    _print(dbmod.seed(seed))


if __name__ == "__main__":
    app()
