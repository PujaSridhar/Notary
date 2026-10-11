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


@app.command()
def baseline(splits: str = "train,heldout,regression,adversarial", k: int = 1,
             limit: int = 0) -> None:
    """Run the agent with the currently active lessons over the given splits."""
    from .runner import active_lessons, run_suite, summarize

    version, lessons = active_lessons()
    query = {"split": {"$in": splits.split(",")}}
    tasks = list(dbmod.db().tasks.find(query).sort("_id", 1))
    if limit:
        tasks = tasks[:limit]
    run_id, traces = run_suite(tasks, lessons, version, k=k)
    _print({"run_id": run_id, "rules_version": version, "lessons": lessons, **summarize(traces)})


if __name__ == "__main__":
    app()
