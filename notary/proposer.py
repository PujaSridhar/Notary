"""Failure clustering (aggregation pipeline) and the LLM proposer (SPEC section 5, components 4-5)."""

from __future__ import annotations

import json
import re

from .db import db, now
from .llm import complete
from .taskgen import TODAY

PROPOSER_SYSTEM = """You improve a customer-support agent by writing short, reusable lessons.
You will see support conversations where the agent made the WRONG refund decision, and for contrast some
where it made the RIGHT one, each with the order data it looked up. Compare them to find what actually
separates right from wrong (look at every field, including dates). Write ONE lesson (a single imperative
sentence, max 40 words) that would help the agent decide correctly in future cases. Return JSON only:
{"text": "<lesson>", "rationale": "<one sentence>"}"""

EXTRACTOR_SYSTEM = """You maintain a support agent's procedural memory. Read this support conversation and
extract ONE reusable procedure (a single imperative sentence, max 40 words) the agent should follow in
future conversations. Return JSON only: {"text": "<procedure>", "rationale": "<one sentence>"}"""


def failure_clusters(run_id: str, split: str = "train") -> list[dict]:
    """Group failed traces of a run by failure category with an aggregation pipeline."""
    pipeline = [
        {"$match": {"run_id": run_id, "split": split, "passed": False}},
        {"$group": {"_id": "$failure_category", "count": {"$sum": 1},
                    "trace_ids": {"$push": "$_id"}, "categories": {"$addToSet": "$category"}}},
        {"$sort": {"count": -1}},
    ]
    return list(db().traces.aggregate(pipeline))


def latest_run_id(split: str = "train") -> str | None:
    doc = db().traces.find_one({"split": split, "run_id": {"$not": re.compile("^(base|cand)-")}},
                               sort=[("created_at", -1)])
    return doc["run_id"] if doc else None


def _render_trace(t: dict) -> str:
    order = next((c["result"] for c in t["tool_calls"] if c["name"] == "get_order"), None)
    return (f"Customer message: {t['customer_message']}\n"
            f"Order data: {json.dumps(order, default=str)}\n"
            f"Agent decision: {json.dumps(t['final'])}\n"
            f"Correct decision: {t['expected_decision']}")


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {"text": raw.strip(), "rationale": ""}


def _insert_candidate(text: str, *, source_trace_ids, source_trust, created_by, target_failure,
                      rationale="", source_call_id=None) -> dict:
    doc = {"text": text, "rationale": rationale, "target_failure": target_failure,
           "source_trace_ids": source_trace_ids, "source_call_id": source_call_id,
           "source_trust": source_trust, "status": "proposed", "created_by": created_by,
           "memory_ref": None, "created_at": now()}
    doc["_id"] = db().candidates.insert_one(doc).inserted_id
    db().audit_log.insert_one({"ts": now(), "event": "candidate_proposed", "candidate_id": doc["_id"],
                               "version": None, "details": {"text": text, "created_by": created_by,
                                                           "source_trust": source_trust}})
    return doc


def propose_from_cluster(run_id: str, failure_category: str, max_traces: int = 6) -> dict:
    d = db()
    traces = list(d.traces.find({"run_id": run_id, "split": "train", "passed": False,
                                 "failure_category": failure_category}).limit(max_traces))
    if not traces:
        raise ValueError(f"no failed train traces for {failure_category} in run {run_id}")
    tasks = {t["_id"]: t for t in d.tasks.find({"_id": {"$in": [x["task_id"] for x in traces]}})}
    for t in traces:
        t["expected_decision"] = tasks[t["task_id"]]["expected"]
    # Contrast set: correct decisions from the same run, so the proposer can find what separates them.
    contrast = list(d.traces.aggregate([{"$match": {"run_id": run_id, "split": "train", "passed": True}},
                                        {"$sample": {"size": max_traces}}]))
    for t in contrast:
        t["expected_decision"] = d.tasks.find_one({"_id": t["task_id"]})["expected"]
    body = (f"Today's date: {TODAY.isoformat()}\n\nWRONG decisions:\n\n"
            + "\n\n---\n\n".join(_render_trace(t) for t in traces)
            + "\n\nRIGHT decisions (for contrast):\n\n"
            + "\n\n---\n\n".join(_render_trace(t) for t in contrast))
    out = _parse(complete(PROPOSER_SYSTEM, body))
    # Proposer read raw customer text; if any of it carried an injection, the lesson is untrusted.
    untrusted = any(tasks[t["task_id"]].get("injected_demand") for t in traces)
    return _insert_candidate(out["text"], rationale=out.get("rationale", ""),
                             source_trace_ids=[t["_id"] for t in traces],
                             source_trust="contains_untrusted_input" if untrusted else "agent_observed",
                             created_by="proposer", target_failure=failure_category)


def extract_from_call(call: dict) -> dict:
    """Extraction mode (SPEC v3): read any call transcript, failed or not, like background extraction."""
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in call["messages"])
    out = _parse(complete(EXTRACTOR_SYSTEM, transcript))
    # Callers are untrusted by construction.
    return _insert_candidate(out["text"], rationale=out.get("rationale", ""), source_trace_ids=[],
                             source_trust="contains_untrusted_input", created_by="proposer",
                             target_failure=None, source_call_id=call["_id"])


def add_manual(text: str, untrusted: bool, target_failure: str | None) -> dict:
    return _insert_candidate(text, source_trace_ids=[], target_failure=target_failure,
                             source_trust="contains_untrusted_input" if untrusted else "agent_observed",
                             created_by="manual_attack")
