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


# ---------------------------------------------------------------------------------------------
# Voice channel (ElevenLabs Agents). ElevenLabs calls this as its custom LLM. Same agent_turn().
# ---------------------------------------------------------------------------------------------
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json as _json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

PUBLIC_PATHS = ("/v1/chat/completions", "/webhooks/elevenlabs")


@app.middleware("http")
async def tunnel_guard(request: Request, call_next):
    """Requests arriving through the public tunnel may only reach the voice endpoints."""
    via_tunnel = "cf-connecting-ip" in request.headers or "x-forwarded-for" in request.headers
    if via_tunnel and not request.url.path.startswith(PUBLIC_PATHS):
        return JSONResponse({"error": "not available over the public tunnel"}, status_code=403)
    if request.url.path.startswith("/v1/"):
        token = os.environ.get("NOTARY_VOICE_TOKEN", "")
        auth_header = request.headers.get("authorization", "")
        bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        header_token = request.headers.get("x-notary-token", "")
        if not token or (header_token != token and bearer_token != token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def _extract_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("text"))
    return str(content or "")


def _voice_conversation_id(messages: list[dict]) -> str:
    """ElevenLabs sends the full history each turn; key the call on its opening turns."""
    opening = [m for m in messages if m.get("role") in ("assistant", "user")][:2]
    return "voice-" + hashlib.sha1(_json.dumps(opening, sort_keys=True).encode()).hexdigest()[:12]


def _voice_turn(messages: list[dict]) -> str:
    turns = []
    for m in messages:
        if m.get("role") in ("user", "assistant"):
            c = _extract_content(m.get("content"))
            if c:
                turns.append({"role": m["role"], "content": c})
    latest = next((m["content"] for m in reversed(turns) if m["role"] == "user"), "")
    if not any(ch.isalnum() for ch in latest):
        # Silence or filler ("...") from speech-to-text: answer briefly, no model call, nothing recorded.
        return "I'm still here. Whenever you're ready, tell me your order number."
    history = "\n".join(f"{'customer' if m['role'] == 'user' else 'agent'}: {m['content']}" for m in turns[:-1])
    prompt = (f"(Phone call. Conversation so far:\n{history}\n)\nCustomer now says: {latest}\n"
              "If you need an order number, ask for it in one short sentence.") if history else latest
    version, lessons = active_lessons()
    loop, final = agent_turn(prompt, lessons)
    reply = _reply_text(final, loop)
    conv = _voice_conversation_id(messages)
    transcript = [{"role": "customer" if m["role"] == "user" else "agent", "content": m["content"]} for m in turns]
    transcript.append({"role": "agent", "content": reply})
    db().calls.update_one(
        {"conversation_id": conv},
        {"$setOnInsert": {"conversation_id": conv, "channel": "voice", "created_at": now()},
         "$set": {"messages": transcript, "updated_at": now()},
         "$push": {"decisions": {"rules_version": version, "final": final, "tool_calls": loop.tool_calls}}},
        upsert=True)
    return reply


def _sse(text: str, model: str, finish_reason: str | None = None) -> str:
    delta = {"content": text} if text else {}
    chunk = {"id": "chatcmpl-notary", "object": "chat.completion.chunk", "created": int(time.time()),
             "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return f"data: {_json.dumps(chunk)}\n\n"


@app.post("/v1/chat/completions")
@app.post("/v1/chat/completions/chat/completions")  # tolerate a full URL configured in ElevenLabs
async def voice_llm(request: Request):
    body = await request.json()
    model = body.get("model", "notary")
    messages = body.get("messages", [])
    stream_requested = body.get("stream", True)

    if not stream_requested:
        try:
            reply = await asyncio.wait_for(run_in_threadpool(_voice_turn, messages), timeout=25)
        except Exception as e:
            print(f"voice_llm error: {e}", flush=True)
            reply = "Sorry, I'm having trouble checking that right now. Could you say the order number again?"
        return {
            "id": "chatcmpl-notary",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    latest = next((_extract_content(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")

    async def stream():
        # Immediate first chunk to satisfy TTFB and start OpenAI SSE stream
        first_chunk = {
            "id": "chatcmpl-notary",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {_json.dumps(first_chunk)}\n\n"

        if any(ch.isdigit() for ch in str(latest)):
            yield _sse("One moment, let me check that order... ", model, finish_reason=None)
        try:
            reply = await asyncio.wait_for(run_in_threadpool(_voice_turn, messages), timeout=25)
        except Exception as e:
            print(f"voice_llm stream error: {e}", flush=True)
            reply = "Sorry, I'm having trouble checking that right now. Could you say the order number again?"
        if reply:
            yield _sse(reply, model, finish_reason=None)
        yield _sse("", model, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/calls")
def recent_calls(limit: int = 8):
    calls = list(db().calls.find({}, {"decisions": 0}).sort("updated_at", -1).limit(limit))
    return _jsonable(calls)
