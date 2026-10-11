"""Re-gate the demo candidates with k repeats against the right baseline, and tag their story roles.

- improvement and loophole were proposed when v0 was live, so they are evaluated against v0.
- overgeneralized, poisoned and organic_poison arrived after the improvement was promoted (v1).
Evaluation only: nothing new is promoted. The run ends with the original active version restored,
and every rollback is recorded in the audit log.

Usage: .venv/bin/python scripts/final_runs.py [k]
"""

import json
import sys
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notary.db import db, now  # noqa: E402
from notary.gate import evaluate  # noqa: E402
from notary.versions import active_state, rollback  # noqa: E402

K = int(sys.argv[1]) if len(sys.argv) > 1 else 3
PLAN = [  # (role, candidate id, baseline version)
    ("improvement", "6acbd328d80c702c62cd8775", 0),
    ("loophole", "6acbd2802e3d1770be81cde0", 0),
    ("overgeneralized", "6acbd363c6b747506fe18b7b", 1),
    ("poisoned", "6acbd39706323332229c80d3", 1),
    ("organic_poison", "6acbd855c70ee769f7ba0863", 1),
]

d = db()
start_version, _ = active_state()
results = {}
try:
    for role, cid, base in PLAN:
        if active_state()[0] != base:
            rollback(base)
        d.candidates.update_one({"_id": ObjectId(cid)}, {"$set": {"demo_role": role}})
        status_before = d.candidates.find_one({"_id": ObjectId(cid)})["status"]
        r = evaluate(cid, k=K, quick=False, auto_promote=False)
        if role == "improvement" and status_before in ("promoted", "rolled_back"):
            # It is already part of v1; an evaluation-only rerun must not change that record.
            d.candidates.update_one({"_id": ObjectId(cid)}, {"$set": {"status": "promoted"}})
        results[role] = {"verdict": r["verdict"], "suites": r["suites"], "reasons": r["reasons"]}
        print(f"[{now():%H:%M:%S}] {role:16} vs v{base} k={K}: {r['verdict']}", flush=True)
        for s, v in r["suites"].items():
            print(f"      {s:12} {v['passed_baseline']:>3} -> {v['passed_candidate']:>3} / {v['n']}   "
                  f"violations {v['violations_baseline']} -> {v['violations_candidate']}", flush=True)
finally:
    if active_state()[0] != start_version:
        rollback(start_version)
    print(f"active version restored to v{active_state()[0]}", flush=True)

Path(__file__).resolve().parent.parent.joinpath("docs", f"final_runs_k{K}.json").write_text(
    json.dumps(results, indent=2, default=str))
