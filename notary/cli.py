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


@app.command()
def clusters(run_id: str = "") -> None:
    """Group failed train traces of a run by failure category (aggregation pipeline)."""
    from .proposer import failure_clusters, latest_run_id

    run_id = run_id or latest_run_id()
    rows = failure_clusters(run_id)
    _print({"run_id": run_id, "clusters": [{"failure_category": r["_id"], "count": r["count"],
                                            "task_categories": r["categories"]} for r in rows]})


@app.command()
def learn(failure: str = "", run_id: str = "") -> None:
    """Propose a candidate lesson from the largest (or given) failure cluster."""
    from .proposer import failure_clusters, latest_run_id, propose_from_cluster

    run_id = run_id or latest_run_id()
    if not failure:
        rows = failure_clusters(run_id)
        if not rows:
            typer.echo("no failures to learn from")
            raise typer.Exit(1)
        failure = rows[0]["_id"]
    c = propose_from_cluster(run_id, failure)
    _print({"candidate_id": c["_id"], "text": c["text"], "rationale": c["rationale"],
            "target_failure": failure, "source_trust": c["source_trust"]})


@app.command()
def add(text: str, untrusted: bool = False, target: str = "") -> None:
    """Insert a candidate by hand (created_by=manual_attack). Always disclosed in the demo."""
    from .proposer import add_manual

    c = add_manual(text, untrusted, target or None)
    _print({"candidate_id": c["_id"], "text": text, "source_trust": c["source_trust"]})


@app.command()
def gate(candidate_id: str, k: int = 1, quick: bool = False) -> None:
    """Run the evaluation gate on a candidate: promoted / rejected / quarantined."""
    from .gate import evaluate

    r = evaluate(candidate_id, k=k, quick=quick)
    _print({"verdict": r["verdict"], "reasons": r["reasons"], "suites": r["suites"],
            "new_version": r.get("new_version"), "eval_run_id": r["_id"]})


@app.command()
def rollback(version: int) -> None:
    """Make an older rule version active again (one transaction, nothing deleted)."""
    from .versions import rollback as do_rollback

    _print(do_rollback(version))


@app.command()
def report(limit: int = 15) -> None:
    """Active version, candidates with verdicts, and the latest audit log entries."""
    d = dbmod.db()
    state = d.memory_state.find_one({"_id": "state"})
    active = d.rule_versions.find_one({"_id": state["active_version"]})
    cands = [{"id": c["_id"], "status": c["status"], "trust": c["source_trust"], "by": c["created_by"],
              "text": c["text"]} for c in d.candidates.find().sort("created_at", 1)]
    audit = [{"ts": a["ts"], "event": a["event"], "version": a.get("version"), "candidate": a.get("candidate_id")}
             for a in d.audit_log.find().sort("ts", -1).limit(limit)]
    _print({"active_version": state["active_version"], "active_rules": [r["text"] for r in active["rules"]],
            "candidates": cands, "audit_log": audit})


if __name__ == "__main__":
    app()
