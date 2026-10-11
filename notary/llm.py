"""Provider-neutral tool-calling loop. Anthropic or OpenAI, chosen by which key is set in .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

from dotenv import load_dotenv

load_dotenv()


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments


@dataclass
class LoopResult:
    messages: list[dict] = field(default_factory=list)  # provider-neutral transcript
    tool_calls: list[dict] = field(default_factory=list)
    stopped_by: str | None = None  # name of the terminal tool, if any
    input_tokens: int = 0
    output_tokens: int = 0


def provider() -> str:
    explicit = os.environ.get("LLM_PROVIDER")
    if explicit:
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    raise RuntimeError("No LLM key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env.")


def model_name() -> str:
    if os.environ.get("LLM_MODEL"):
        return os.environ["LLM_MODEL"]
    return "claude-haiku-5-5" if provider() == "anthropic" else "gpt-4.1-mini"


def run_tool_loop(system: str, user: str, tools: list[ToolSpec],
                  call_tool: Callable[[str, dict], dict], terminal: set[str],
                  max_steps: int = 8, temperature: float = 0.0) -> LoopResult:
    """Run the model with tools until it calls a terminal tool, stops, or hits max_steps."""
    if provider() == "anthropic":
        return _anthropic_loop(system, user, tools, call_tool, terminal, max_steps, temperature)
    return _openai_loop(system, user, tools, call_tool, terminal, max_steps, temperature)


def proposer_model() -> str:
    """The proposer can use a stronger model than the agent; the agent stays cheap and fast."""
    if os.environ.get("PROPOSER_MODEL"):
        return os.environ["PROPOSER_MODEL"]
    return "claude-sonnet-5-5" if provider() == "anthropic" else "gpt-5.5"


def complete(system: str, user: str, temperature: float = 0.0, max_tokens: int = 1200,
             model: str | None = None) -> str:
    """Plain text completion (used by the proposer)."""
    if provider() == "anthropic":
        import anthropic
        resp = anthropic.Anthropic().messages.create(
            model=model or proposer_model(), max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if b.type == "text")
    import openai
    model = model or proposer_model()
    # GPT-5 / o-series reasoning models take no temperature and count reasoning in completion tokens.
    reasoning = model.startswith(("gpt-5", "o3", "o4"))
    extra = {"max_completion_tokens": 8000} if reasoning else {"temperature": temperature, "max_tokens": max_tokens}
    resp = openai.OpenAI().chat.completions.create(
        model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **extra)
    return resp.choices[0].message.content or ""


def _anthropic_loop(system, user, tools, call_tool, terminal, max_steps, temperature) -> LoopResult:
    import anthropic

    client = anthropic.Anthropic()
    spec = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
    msgs: list[dict] = [{"role": "user", "content": user}]
    out = LoopResult(messages=[{"role": "user", "content": user}])
    for _ in range(max_steps):
        resp = client.messages.create(model=model_name(), max_tokens=1024, temperature=temperature,
                                      system=system, tools=spec, messages=msgs)
        out.input_tokens += resp.usage.input_tokens
        out.output_tokens += resp.usage.output_tokens
        msgs.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        if text:
            out.messages.append({"role": "assistant", "content": text})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            return out
        results = []
        for u in uses:
            result = call_tool(u.name, dict(u.input))
            out.tool_calls.append({"name": u.name, "args": dict(u.input), "result": result})
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(result, default=str)})
            if u.name in terminal:
                out.stopped_by = u.name
        if out.stopped_by:
            return out
        msgs.append({"role": "user", "content": results})
    return out


def _openai_loop(system, user, tools, call_tool, terminal, max_steps, temperature) -> LoopResult:
    import openai

    client = openai.OpenAI()
    spec = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                               "parameters": t.parameters}} for t in tools]
    msgs: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    out = LoopResult(messages=[{"role": "user", "content": user}])
    for _ in range(max_steps):
        resp = client.chat.completions.create(model=model_name(), temperature=temperature,
                                              tools=spec, messages=msgs)
        if resp.usage:
            out.input_tokens += resp.usage.prompt_tokens
            out.output_tokens += resp.usage.completion_tokens
        m = resp.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if m.content:
            out.messages.append({"role": "assistant", "content": m.content})
        if not m.tool_calls:
            return out
        for tc in m.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = call_tool(tc.function.name, args)
            out.tool_calls.append({"name": tc.function.name, "args": args, "result": result})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)})
            if tc.function.name in terminal:
                out.stopped_by = tc.function.name
        if out.stopped_by:
            return out
    return out
