"""Run the agent over task suites in parallel, grade, and write traces to Atlas."""

from __future__ import annotations

import os
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .agent import agent_turn
from .db import db, now
from .grader import grade
from .taskgen import TODAY, order_from_doc


def active_lessons() -> tuple[int, list[str]]:
    d = db()
    state = d.memory_state.find_one({"_id": "state"}) or {"active_version": 0}
    ver_num = state["active_version"]
    version = d.rule_versions.find_one({"_id": ver_num}) or {"rules": []}
    fallback_texts = [r["text"] for r in version.get("rules", [])]
    if not fallback_texts:
        return ver_num, []
    try:
        from .memory import store
        active_ids = [r["candidate_id"] for r in version.get("rules", [])]
        lessons = store().lessons_for("refund", active_ids)
        if lessons:
            return ver_num, lessons
    except Exception:
        pass
    return ver_num, fallback_texts


def run_one(task: dict, lessons: list[str], run_id: str, rules_version, rep: int) -> dict:
    order = order_from_doc(db().orders.find_one({"_id": task["order_id"]}))
    message = f"[Customer {task['customer_id']}] {task['customer_message']}"
    try:
        loop, final = agent_turn(message, lessons)
        error = None
    except Exception as e:  # keep the suite running; a crashed run counts as no decision
        loop, final, error = None, None, repr(e)
    g = grade(task, order, TODAY, final)
    trace = {
        "task_id": task["_id"], "split": task["split"], "category": task["category"],
        "run_id": run_id, "rep": rep, "rules_version": rules_version,
        "messages": loop.messages if loop else [], "tool_calls": loop.tool_calls if loop else [],
        "final": final, "passed": g.passed, "failure_category": g.failure_category,
        "invariants_violated": g.invariants_violated, "error": error,
        "tokens": {"in": loop.input_tokens, "out": loop.output_tokens} if loop else None,
        "customer_message": task["customer_message"], "created_at": now(),
    }
    db().traces.insert_one(trace)
    return trace


def run_suite(tasks: list[dict], lessons: list[str], rules_version, k: int = 1,
              workers: int | None = None, run_id: str | None = None) -> tuple[str, list[dict]]:
    run_id = run_id or uuid.uuid4().hex[:10]
    workers = workers or int(os.environ.get("NOTARY_WORKERS", "12"))
    jobs = [(t, rep) for rep in range(k) for t in tasks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        traces = list(pool.map(lambda j: run_one(j[0], lessons, run_id, rules_version, j[1]), jobs))
    return run_id, traces


def summarize(traces: list[dict]) -> dict:
    by_split: dict = defaultdict(lambda: {"n": 0, "passed": 0, "violations": 0})
    failures = Counter()
    for t in traces:
        s = by_split[t["split"]]
        s["n"] += 1
        s["passed"] += int(t["passed"])
        s["violations"] += len(t["invariants_violated"])
        if t["failure_category"]:
            failures[t["failure_category"]] += 1
    errors = sum(1 for t in traces if t.get("error"))
    return {"by_split": dict(by_split), "failure_categories": dict(failures), "errors": errors}
