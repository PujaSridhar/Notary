"""Versioned promotion and rollback, each in one multi-document transaction (SPEC section 5, component 7)."""

from __future__ import annotations

from .db import client, db, now
from .memory import store


def active_state() -> tuple[int, dict]:
    d = db()
    state = d.memory_state.find_one({"_id": "state"})
    return state["active_version"], d.rule_versions.find_one({"_id": state["active_version"]})


def promote(candidate: dict, eval_run_id) -> int:
    d = db()
    memory_ref = store().write_lesson(candidate)  # trusted memory write: only ever called from here
    with client().start_session() as s:
        with s.start_transaction():
            state = d.memory_state.find_one({"_id": "state"}, session=s)
            current = d.rule_versions.find_one({"_id": state["active_version"]}, session=s)
            new_version = (d.rule_versions.find_one({}, sort=[("_id", -1)], session=s)["_id"]) + 1
            rules = current["rules"] + [{"candidate_id": candidate["_id"], "text": candidate["text"]}]
            d.rule_versions.insert_one({"_id": new_version, "rules": rules,
                                        "parent_version": current["_id"],
                                        "promoted_candidate_id": candidate["_id"],
                                        "eval_run_id": eval_run_id, "created_at": now()}, session=s)
            d.memory_state.update_one({"_id": "state"},
                                      {"$set": {"active_version": new_version, "updated_at": now()}},
                                      session=s)
            d.candidates.update_one({"_id": candidate["_id"]},
                                    {"$set": {"status": "promoted", "memory_ref": memory_ref}}, session=s)
            d.audit_log.insert_one({"ts": now(), "event": "promoted", "candidate_id": candidate["_id"],
                                    "version": new_version,
                                    "details": {"from_version": current["_id"], "eval_run_id": eval_run_id}},
                                   session=s)
    return new_version


def rollback(to_version: int) -> dict:
    """Point memory_state back at an older version. Nothing is deleted; rolled-back lessons stop being served."""
    d = db()
    with client().start_session() as s:
        with s.start_transaction():
            target = d.rule_versions.find_one({"_id": to_version}, session=s)
            if target is None:
                raise ValueError(f"version {to_version} does not exist")
            state = d.memory_state.find_one({"_id": "state"}, session=s)
            current = d.rule_versions.find_one({"_id": state["active_version"]}, session=s)
            kept = {r["candidate_id"] for r in target["rules"]}
            dropped = [r["candidate_id"] for r in current["rules"] if r["candidate_id"] not in kept]
            d.memory_state.update_one({"_id": "state"},
                                      {"$set": {"active_version": to_version, "updated_at": now()}}, session=s)
            if dropped:
                d.candidates.update_many({"_id": {"$in": dropped}}, {"$set": {"status": "rolled_back"}},
                                         session=s)
            if kept:  # rolling forward re-activates lessons the target version contains
                d.candidates.update_many({"_id": {"$in": list(kept)}}, {"$set": {"status": "promoted"}},
                                         session=s)
            d.audit_log.insert_one({"ts": now(), "event": "rolled_back", "candidate_id": None,
                                    "version": to_version,
                                    "details": {"from_version": current["_id"], "dropped": dropped}},
                                   session=s)
    return {"from_version": current["_id"], "to_version": to_version, "dropped": dropped}
