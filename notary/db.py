"""Atlas Sandbox access: connection, indexes, seeding (SPEC section 6)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

load_dotenv()

COLLECTIONS = ["customers", "orders", "tasks", "traces", "candidates", "eval_runs",
               "rule_versions", "memory_state", "audit_log", "calls"]


@lru_cache(maxsize=1)
def client() -> MongoClient:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set. Put it in .env (see .env.example).")
    return MongoClient(uri, appname="notary", serverSelectionTimeoutMS=10000)


def db() -> Database:
    return client()[os.environ.get("MONGODB_DB", "notary")]


def now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(d: Database) -> None:
    d.traces.create_index([("task_id", ASCENDING), ("run_id", ASCENDING)])
    d.traces.create_index([("failure_category", ASCENDING)])
    d.traces.create_index([("run_id", ASCENDING), ("passed", ASCENDING)])
    d.candidates.create_index([("status", ASCENDING)])
    d.eval_runs.create_index([("candidate_id", ASCENDING)])
    d.tasks.create_index([("split", ASCENDING), ("category", ASCENDING)])
    d.audit_log.create_index([("ts", ASCENDING)])
    d.calls.create_index([("conversation_id", ASCENDING)], unique=True, sparse=True)


def check() -> dict:
    """Ping the cluster and confirm multi-document transactions work (SPEC section 12)."""
    c = client()
    c.admin.command("ping")
    d = db()
    probe = d["_txn_probe"]
    with c.start_session() as s:
        with s.start_transaction():
            probe.insert_one({"ts": now()}, session=s)
            probe.delete_many({}, session=s)
    probe.drop()
    return {"ping": "ok", "transactions": "ok", "db": d.name,
            "collections": sorted(d.list_collection_names())}


def seed(seed_value: int = 7) -> dict:
    from .taskgen import generate, task_doc

    d = db()
    customers, orders, tasks = generate(seed_value)
    for name in ("customers", "orders", "tasks"):
        d[name].delete_many({})
    d.customers.insert_many(customers)
    d.orders.insert_many(orders)
    d.tasks.insert_many([task_doc(t) for t in tasks])
    if d.memory_state.count_documents({}) == 0:
        d.rule_versions.insert_one({"_id": 0, "rules": [], "parent_version": None,
                                    "promoted_candidate_id": None, "created_at": now()})
        d.memory_state.insert_one({"_id": "state", "active_version": 0, "updated_at": now()})
        d.audit_log.insert_one({"ts": now(), "event": "initialized", "version": 0,
                                "candidate_id": None, "details": {"seed": seed_value}})
    ensure_indexes(d)
    counts = {s: d.tasks.count_documents({"split": s})
              for s in ("train", "heldout", "regression", "adversarial")}
    return {"customers": len(customers), "orders": len(orders), "tasks": counts}
