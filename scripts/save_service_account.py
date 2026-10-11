"""Read `agentengine service-account create --json` output from stdin and store the credential in .env.

The client secret is written to .env (gitignored) and never printed.
Usage:
  agentengine service-account create notary-memory --role PROJECT_OWNER --context notary --json \
    | .venv/bin/python scripts/save_service_account.py
"""

import json
import re
import sys
from pathlib import Path


def find(obj, pattern):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if re.search(pattern, k, re.I) and isinstance(v, str):
                return v
            found = find(v, pattern)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = find(v, pattern)
            if found:
                return found
    return None


raw = sys.stdin.read()
data = json.loads(raw[raw.find("{"):])
client_id = find(data, r"client_?id")
secret = find(data, r"client_?secret|^secret$")
if not client_id or not secret:
    sys.exit(f"Could not find client id/secret in the output. Keys seen: {list(data)[:10]}")

env = Path(__file__).resolve().parent.parent / ".env"
lines = [l for l in env.read_text().splitlines() if not l.startswith(("AE_CLIENT_ID=", "AE_CLIENT_SECRET="))]
lines += [f"AE_CLIENT_ID={client_id}", f"AE_CLIENT_SECRET={secret}"]
env.write_text("\n".join(lines) + "\n")
print(f"Saved service account {client_id} to .env (secret hidden).")
