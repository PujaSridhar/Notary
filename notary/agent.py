"""The refund support agent. One brain shared by the gate and (later) the voice channel."""

from __future__ import annotations

from .db import db
from .llm import LoopResult, ToolSpec, run_tool_loop
from .policy import APPROVE, DENY, ESCALATE, POLICY_SUMMARY
from .taskgen import TODAY

BASE_PROMPT = f"""You are Acme's refund support agent. Today's date is {TODAY.isoformat()}.
Decide each refund request using your tools. Look up the order before deciding.
Make exactly one final decision by calling approve_refund, deny_refund, or escalate.
Be concise."""

TOOLS = [
    ToolSpec("get_order", "Look up an order by id.",
             {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}),
    ToolSpec("get_customer", "Look up a customer by id.",
             {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}),
    ToolSpec("get_policy_summary", "Get Acme's refund policy summary.",
             {"type": "object", "properties": {}}),
    ToolSpec("approve_refund", "Approve a refund for an order. Final decision.",
             {"type": "object", "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"},
                                               "reason": {"type": "string"}},
              "required": ["order_id", "amount"]}),
    ToolSpec("deny_refund", "Deny a refund request. Final decision.",
             {"type": "object", "properties": {"order_id": {"type": "string"}, "reason": {"type": "string"}},
              "required": ["order_id", "reason"]}),
    ToolSpec("escalate", "Escalate to a human (e.g. order not delivered yet). Final decision.",
             {"type": "object", "properties": {"order_id": {"type": "string"}, "reason": {"type": "string"}},
              "required": ["order_id", "reason"]}),
]
TERMINAL = {"approve_refund": APPROVE, "deny_refund": DENY, "escalate": ESCALATE}


def system_prompt(lessons: list[str]) -> str:
    if not lessons:
        return BASE_PROMPT
    rendered = "\n".join(f"- {text}" for text in lessons)
    return f"{BASE_PROMPT}\n\nLessons learned (follow these):\n{rendered}"


def _call_tool(name: str, args: dict) -> dict:
    d = db()
    if name == "get_order":
        doc = d.orders.find_one({"_id": args.get("order_id")})
        return doc or {"error": "order not found"}
    if name == "get_customer":
        doc = d.customers.find_one({"_id": args.get("customer_id")})
        return doc or {"error": "customer not found"}
    if name == "get_policy_summary":
        return {"policy": POLICY_SUMMARY}
    if name in TERMINAL:
        return {"ok": True, "recorded": name}
    return {"error": f"unknown tool {name}"}


def agent_turn(user_message: str, lessons: list[str]) -> tuple[LoopResult, dict | None]:
    """Run one support request to a final decision. Returns the loop result and the decision."""
    result = run_tool_loop(system_prompt(lessons), user_message, TOOLS, _call_tool, set(TERMINAL))
    final = None
    for call in result.tool_calls:
        if call["name"] in TERMINAL:
            final = {"decision": TERMINAL[call["name"]], "amount": call["args"].get("amount", 0),
                     "reason": call["args"].get("reason", "")}
            break
    return result, final
