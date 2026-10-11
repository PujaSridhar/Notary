"""Notary web service: the review page, the customer chat channel, and (later) the voice channel."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .agent import agent_turn
from .db import db, now
from .gate import evaluate
from .proposer import extract_from_call
from .runner import active_lessons, run_suite
from .taskgen import TODAY, order_from_doc
from .versions import rollback

app = FastAPI(title="Notary")
STATIC = Path(__file__).parent / "static"
_jobs: dict[str, dict] = {}


def _jsonable(x):
    if isinstance(x, ObjectId):
        return str(x)
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_jsonable(v) for v in x]
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


@app.get("/")
def page():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    d = db()
    st = d.memory_state.find_one({"_id": "state"})
    versions = list(d.rule_versions.find().sort("_id", 1))
    cands = []
    for c in d.candidates.find().sort("created_at", -1):
        ev = d.eval_runs.find_one({"candidate_id": c["_id"]}, sort=[("created_at", -1)])
        cands.append({**c, "eval": ev})
    audit = list(d.audit_log.find().sort("ts", -1).limit(30))
    return _jsonable({"active_version": st["active_version"], "versions": versions,
                      "candidates": cands, "audit": audit,
                      "ungated": d.ungated_memory.find_one({}, sort=[("written_at", -1)])})


@app.get("/api/demo-orders")
def demo_orders():
    """A few orders for people to try in the chat, with the facts the policy depends on."""
    d = db()
    out = []
    for cat in ("gold_out_of_window", "defective_exception", "in_window", "final_sale"):
        t = d.tasks.find_one({"split": "train", "category": cat})
        o = d.orders.find_one({"_id": t["order_id"]})
        c = d.customers.find_one({"_id": o["customer_id"]})
        days = (TODAY - order_from_doc(o).delivered_at).days if o["delivered_at"] else None
        out.append({"order_id": o["_id"], "customer_id": c["_id"], "tier": c["tier"], "total": o["total"],
                    "days_since_delivery": days, "defective": o["defective"], "final_sale": o["final_sale"],
                    "label": cat.replace("_", " ")})
    return out


class ChatIn(BaseModel):
    message: str
    customer_id: str = "C0000"
    conversation_id: str | None = None


def _reply_text(final: dict | None, loop) -> str:
    texts = [m["content"] for m in loop.messages if m["role"] == "assistant" and m.get("content")]
    if final is None:
        return texts[-1] if texts else "Sorry, could you share your order number?"
    if final["decision"] == "approve":
        return f"I've approved a refund of ${float(final['amount']):.2f}. {final.get('reason', '')}".strip()
    if final["decision"] == "deny":
        return f"I'm sorry, I can't approve this refund. {final.get('reason', '')}".strip()
    return f"I've escalated this to a specialist. {final.get('reason', '')}".strip()


def handle_customer_turn(message: str, customer_id: str, channel: str,
                         conversation_id: str | None = None) -> dict:
    """One brain for every channel: the same agent_turn() the gate evaluates."""
    version, lessons = active_lessons()
    loop, final = agent_turn(f"[Customer {customer_id}] {message}", lessons)
    reply = _reply_text(final, loop)
    conv = conversation_id or uuid.uuid4().hex[:12]
    d = db()
    d.calls.update_one(
        {"conversation_id": conv},
        {"$setOnInsert": {"conversation_id": conv, "channel": channel, "created_at": now(),
                          "customer_id": customer_id},
         "$push": {"messages": {"$each": [{"role": "customer", "content": message},
                                          {"role": "agent", "content": reply}]},
                   "decisions": {"rules_version": version, "final": final,
                                 "tool_calls": loop.tool_calls}},
         "$set": {"updated_at": now()}},
        upsert=True)
    return {"conversation_id": conv, "reply": reply, "final": final, "rules_version": version}


@app.post("/api/chat")
def chat(body: ChatIn):
    return handle_customer_turn(body.message, body.customer_id, "web_chat", body.conversation_id)


def _run_job(job_id: str, fn, *args, **kw):
    try:
        _jobs[job_id] = {"status": "running"}
        _jobs[job_id] = {"status": "done", "result": _jsonable(fn(*args, **kw))}
    except Exception as e:  # surfaced to the page
        _jobs[job_id] = {"status": "error", "error": repr(e)}


def _start(fn, *args, **kw) -> dict:
    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "queued"}
    threading.Thread(target=_run_job, args=(job_id, fn, *args), kwargs=kw, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404)
    return _jobs[job_id]


def _extract_and_gate(conversation_id: str, quick: bool) -> dict:
    call = db().calls.find_one({"conversation_id": conversation_id})
    if call is None:
        raise ValueError("conversation not found")
    cand = extract_from_call(call)
    result = evaluate(cand["_id"], k=1, quick=quick)
    return {"candidate": cand, "eval": result}


@app.post("/api/calls/{conversation_id}/extract-and-gate")
def extract_and_gate(conversation_id: str, quick: bool = True):
    return _start(_extract_and_gate, conversation_id, quick)


def _ungated(candidate_id: str) -> dict:
    """The naive path, for comparison only: write the lesson straight into a separate 'ungated' memory
    (standing in for unchecked background extraction) and run the agent with it. Trusted memory is untouched."""
    d = db()
    cand = d.candidates.find_one({"_id": ObjectId(candidate_id)})
    d.ungated_memory.insert_one({"candidate_id": cand["_id"], "text": cand["text"], "written_at": now()})
    _, lessons = active_lessons()
    tasks = list(d.tasks.find({"split": {"$in": ["regression", "adversarial"]}}).sort("_id", 1))
    _, traces = run_suite(tasks, lessons + [cand["text"]], "ungated", k=1, run_id=f"ungated-{candidate_id}")
    harmed = [t for t in traces if t["invariants_violated"]]
    return {"candidate_id": candidate_id, "n": len(traces), "passed": sum(t["passed"] for t in traces),
            "violations": sum(len(t["invariants_violated"]) for t in traces),
            "examples": [{"task_id": t["task_id"], "category": t["category"], "final": t["final"],
                          "invariants": t["invariants_violated"]} for t in harmed[:5]]}


@app.post("/api/candidates/{candidate_id}/ungated")
def ungated(candidate_id: str):
    return _start(_ungated, candidate_id)


class RollbackIn(BaseModel):
    version: int


@app.post("/api/rollback")
def do_rollback(body: RollbackIn):
    return _jsonable(rollback(body.version))
