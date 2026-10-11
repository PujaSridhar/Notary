"""Create or re-point the ElevenLabs voice agent at Notary's custom-LLM endpoint.

Usage:
  .venv/bin/python scripts/voice_agent.py https://<your-tunnel>.trycloudflare.com
Creates the agent on first run (saves ELEVENLABS_AGENT_ID to .env); later runs update its URL,
which is needed whenever the temporary tunnel URL changes.
"""

import json
import sys
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

ENV = Path(__file__).resolve().parent.parent / ".env"
env = dotenv_values(ENV)
base = sys.argv[1].rstrip("/")
# ElevenLabs appends /chat/completions to this base URL itself.
custom_llm = {"url": f"{base}/v1", "model_id": "notary",
              "request_headers": {"X-Notary-Token": env["NOTARY_VOICE_TOKEN"]}}
agent = {"first_message": "Hi, this is Acme refund support. This call is recorded and transcribed. How can I help you today?", "language": "en",
         "prompt": {"prompt": "You are Acme's refund support agent. (Decisions are made by the Notary custom LLM.)",
                    "llm": "custom-llm", "custom_llm": custom_llm}}


def call(method: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"https://api.elevenlabs.io{path}", method=method, data=json.dumps(body).encode(),
                                 headers={"xi-api-key": env["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


agent_id = env.get("ELEVENLABS_AGENT_ID")
if agent_id:
    call("PATCH", f"/v1/convai/agents/{agent_id}", {"conversation_config": {"agent": agent}})
    print(f"Updated {agent_id} -> {custom_llm['url']}")
else:
    agent_id = call("POST", "/v1/convai/agents/create",
                    {"name": "Notary - Acme Refund Desk", "conversation_config": {"agent": agent}})["agent_id"]
    lines = [l for l in ENV.read_text().splitlines() if not l.startswith("ELEVENLABS_AGENT_ID=")]
    ENV.write_text("\n".join(lines + [f"ELEVENLABS_AGENT_ID={agent_id}"]) + "\n")
    print(f"Created {agent_id}")
print(f"Talk to it: https://elevenlabs.io/app/talk-to?agent_id={agent_id}")
