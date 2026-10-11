"""Add boundary-day tasks (around the 30- and 90-day windows) to the train, held-out and regression suites.

Motivated by the independent benchmark, which showed the agent mis-estimating day counts near the windows.
The independent tasks themselves are never copied: these are new orders with different days and messages.
Expected decisions come only from notary/policy.py. Idempotent: re-running replaces only these tasks.
"""

import random
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notary.db import db  # noqa: E402
from notary.policy import Order, expected_decision  # noqa: E402
from notary.taskgen import TODAY, order_doc  # noqa: E402

# (split, days since delivery, defective)
PLAN = [
    ("train", 29, False), ("train", 31, False), ("train", 33, False), ("train", 88, True), ("train", 92, True),
    ("train", 94, True),
    ("heldout", 30, False), ("heldout", 32, False), ("heldout", 34, False), ("heldout", 89, True),
    ("heldout", 91, True), ("heldout", 93, True), ("heldout", 96, True), ("heldout", 64, False),
    ("regression", 28, False), ("regression", 35, False), ("regression", 87, True), ("regression", 90, True),
    ("regression", 97, True), ("regression", 71, False),
]
MESSAGES = [
    "Hi, order {oid} arrived a while back and {issue}. Can I get my money back?",
    "Hello, I'd like to return order {oid}. {issue_cap}. I think I'm still within the return period.",
    "Order {oid}: {issue}. Please refund me, it hasn't been that long since it was delivered.",
    "Hey, about order {oid} - {issue}. What are my options for a refund?",
]


def main() -> None:
    d = db()
    rng = random.Random(2026)
    d.tasks.delete_many({"category": "boundary_days", "split": {"$in": ["train", "heldout", "regression"]}})
    d.orders.delete_many({"_id": {"$regex": "^B1"}})
    d.customers.delete_many({"_id": {"$regex": "^BC"}})
    counts: dict[str, int] = {}
    for n, (split, days, defective) in enumerate(PLAN, start=1):
        oid, cid = f"B1{n:04d}", f"BC{n:04d}"
        delivered = TODAY - timedelta(days=days)
        order = Order(oid, cid, round(rng.uniform(20, 350), 2), delivered - timedelta(days=rng.randint(2, 6)),
                      delivered, defective, False)
        issue = "it stopped working properly, it's defective" if defective else "it's just not what I wanted"
        msg = rng.choice(MESSAGES).format(oid=oid, issue=issue, issue_cap=issue[0].upper() + issue[1:])
        exp = expected_decision(order, TODAY)
        d.customers.insert_one({"_id": cid, "name": f"Boundary customer {n}", "tier": rng.choice(["standard", "silver"])})
        d.orders.insert_one(order_doc(order))
        d.tasks.insert_one({"_id": f"BT{n:04d}", "split": split, "category": "boundary_days", "order_id": oid,
                            "customer_id": cid, "customer_message": msg, "requested_amount": None,
                            "expected": {"decision": exp.decision, "max_amount": exp.max_amount},
                            "invariants": [1, 2, 3, 4], "injected_demand": None, "seed": 2026,
                            "meta": {"days_since_delivery": days, "defective": defective}})
        counts[split] = counts.get(split, 0) + 1
    print("added boundary tasks:", counts)


if __name__ == "__main__":
    main()
