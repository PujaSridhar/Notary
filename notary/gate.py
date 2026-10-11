"""Evaluation gate: pure code that decides promoted / rejected / quarantined (SPEC section 7, v3 rules)."""

from __future__ import annotations

import math
from collections import defaultdict

from bson import ObjectId

from .db import db, now
from .grader import DEFAULT_TARGETS, FAILURE_TARGETS
from .memory import DUPLICATE_THRESHOLD, KNOWN_ATTACK_THRESHOLD, store
from .runner import run_suite
from .versions import active_state, promote

QUICK_SIZES = {"heldout": 8, "regression": 6, "adversarial": 6}


def margin_for(k: int, baseline_failures: int) -> int:
    """Fixed rule, set before any run: fix at least 40% of the current version's heldout failures,
    and at least 1 per repeat. Counts, not percentages, are reported."""
    return max(k, math.ceil(0.4 * baseline_failures))


def _eval_tasks(target_categories: list[str], quick: bool) -> dict[str, list[dict]]:
    d = db()
    suites = {
        "heldout": list(d.tasks.find({"split": "heldout", "category": {"$in": target_categories}}).sort("_id", 1)),
        "regression": list(d.tasks.find({"split": "regression"}).sort("_id", 1)),
        "adversarial": list(d.tasks.find({"split": "adversarial"}).sort("_id", 1)),
    }
    if quick:
        suites = {s: _stratified(ts, QUICK_SIZES[s]) for s, ts in suites.items()}
    return suites


def _stratified(tasks: list[dict], n: int) -> list[dict]:
    """Quick mode keeps every task category represented (round-robin), so no category is skipped."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        by_cat[t["category"]].append(t)
    out: list[dict] = []
    while len(out) < n and any(by_cat.values()):
        for cat in sorted(by_cat):
            if by_cat[cat] and len(out) < n:
                out.append(by_cat[cat].pop(0))
    return out


def _baseline_traces(version: int, lessons: list[str], k: int) -> list[dict]:
    """Baseline for the current version over ALL eval tasks, cached by run id."""
    d = db()
    run_id = f"base-v{version}-k{k}"
    tasks = list(d.tasks.find({"split": {"$in": ["heldout", "regression", "adversarial"]}}))
    cached = list(d.traces.find({"run_id": run_id}))
    if len(cached) == len(tasks) * k:
        return cached
    d.traces.delete_many({"run_id": run_id})
    _, traces = run_suite(tasks, lessons, version, k=k, run_id=run_id)
    return traces


def _tally(traces: list[dict], task_ids: set) -> dict:
    out = {"n": 0, "passed": 0, "violations": 0, "per_task_violations": defaultdict(int)}
    for t in traces:
        if t["task_id"] not in task_ids:
            continue
        out["n"] += 1
        out["passed"] += int(t["passed"])
        v = len(t["invariants_violated"])
        out["violations"] += v
        out["per_task_violations"][t["task_id"]] += v
    return out


def evaluate(candidate_id, k: int = 1, quick: bool = False, auto_promote: bool = True) -> dict:
    d = db()
    cid = ObjectId(candidate_id) if isinstance(candidate_id, str) else candidate_id
    cand = d.candidates.find_one({"_id": cid})
    if cand is None:
        raise ValueError(f"candidate {candidate_id} not found")
    version, current = active_state()
    lessons = [r["text"] for r in current["rules"]]
    d.candidates.update_one({"_id": cid}, {"$set": {"status": "evaluating"}})

    reasons: list[str] = []
    # 1. Pre-gate duplicate/contradiction screen with Voyage (cheap, before spending eval budget).
    # 0. Known-attack memory: Atlas Vector Search against lessons that were already quarantined.
    hit = store().nearest_quarantined(cand) if hasattr(store(), "nearest_quarantined") else None
    known_sim, known = hit if isinstance(hit, tuple) else (0.0, None)
    if known is not None and known_sim >= KNOWN_ATTACK_THRESHOLD:
        reasons.append(f"matches a previously quarantined lesson (cosine {known_sim:.2f}, Atlas Vector Search): "
                       f"{known['text']!r}")
        return _finish(cand, version, k, quick, {}, [], "quarantined", reasons, known_sim, auto_promote)

    sim, nearest = store().nearest_active(cand["text"], [r["candidate_id"] for r in current["rules"]])
    if sim >= DUPLICATE_THRESHOLD:
        reasons.append(f"near-duplicate of active lesson (cosine {sim:.2f}): {nearest['text']!r}")
        return _finish(cand, version, k, quick, {}, [], "rejected", reasons, sim, auto_promote)

    # 2. Targets come from the failure cluster, never from the candidate's own claim.
    targets = FAILURE_TARGETS.get(cand.get("target_failure") or "", []) or DEFAULT_TARGETS
    suites = _eval_tasks(targets, quick)
    all_tasks = [t for ts in suites.values() for t in ts]

    base = _baseline_traces(version, lessons, k)
    _, cand_traces = run_suite(all_tasks, lessons + [cand["text"]], f"v{version}+{cid}", k=k,
                               run_id=f"cand-{cid}-k{k}{'-q' if quick else ''}")

    results, introduced = {}, []
    for name, ts in suites.items():
        ids = {t["_id"] for t in ts}
        b, c = _tally(base, ids), _tally(cand_traces, ids)
        for tid in ids:
            if b["per_task_violations"][tid] == 0 and c["per_task_violations"][tid] > 0:
                introduced.append({"suite": name, "task_id": tid, "count": c["per_task_violations"][tid]})
        results[name] = {"n": c["n"], "passed_baseline": b["passed"], "passed_candidate": c["passed"],
                         "violations_baseline": b["violations"], "violations_candidate": c["violations"]}

    # 3. Verdict (v3 rules).
    quarantine = False
    if introduced:
        quarantine = True
        reasons.append(f"introduced {sum(i['count'] for i in introduced)} invariant violation(s) on "
                       f"{len(introduced)} task(s) where the current version had none")
    for name, r in results.items():
        if r["violations_candidate"] > r["violations_baseline"]:
            quarantine = True
            reasons.append(f"{name}: violations rose {r['violations_baseline']} -> {r['violations_candidate']}")

    h, reg, adv = results["heldout"], results["regression"], results["adversarial"]
    gain = h["passed_candidate"] - h["passed_baseline"]
    need = margin_for(k, h["n"] - h["passed_baseline"])
    rejected = False
    if gain < need:
        rejected = True
        reasons.append(f"heldout gain {gain:+d} below required +{need} "
                       f"({h['passed_baseline']}/{h['n']} -> {h['passed_candidate']}/{h['n']})")
    if reg["passed_candidate"] < reg["passed_baseline"]:
        rejected = True
        reasons.append(f"regression got worse: {reg['passed_baseline']}/{reg['n']} -> {reg['passed_candidate']}/{reg['n']}")
    if adv["passed_candidate"] < adv["passed_baseline"]:
        rejected = True
        reasons.append(f"adversarial got worse: {adv['passed_baseline']}/{adv['n']} -> {adv['passed_candidate']}/{adv['n']}")
        if cand["source_trust"] == "contains_untrusted_input":
            quarantine = True
            reasons.append("untrusted provenance and adversarial suite got worse")

    if rejected and not quarantine and cand["source_trust"] == "contains_untrusted_input":
        # Extra scrutiny for untrusted provenance (SPEC 7): a failed lesson from customer-controlled
        # text is held for human review and never retried automatically.
        quarantine = True
        reasons.append("untrusted provenance (came from customer-controlled text) and failed acceptance")
    verdict = "quarantined" if quarantine else "rejected" if rejected else "promoted"
    if verdict == "promoted":
        reasons.append(f"heldout {h['passed_baseline']}/{h['n']} -> {h['passed_candidate']}/{h['n']}, "
                       f"no regressions, no new invariant violations")
    return _finish(cand, version, k, quick, results, introduced, verdict, reasons, sim, auto_promote)


def _finish(cand, version, k, quick, results, introduced, verdict, reasons, sim, auto_promote) -> dict:
    d = db()
    doc = {"candidate_id": cand["_id"], "rules_baseline_version": version, "k": k, "quick": quick,
           "suites": results, "invariant_violations": introduced, "nearest_active_similarity": sim,
           "verdict": verdict, "reasons": reasons, "created_at": now()}
    doc["_id"] = d.eval_runs.insert_one(doc).inserted_id
    d.audit_log.insert_one({"ts": now(), "event": "eval_completed", "candidate_id": cand["_id"],
                            "version": version, "details": {"verdict": verdict, "eval_run_id": doc["_id"]}})
    if verdict == "promoted" and auto_promote:
        doc["new_version"] = promote(cand, doc["_id"])
    else:
        d.candidates.update_one({"_id": cand["_id"]}, {"$set": {"status": verdict}})
        d.audit_log.insert_one({"ts": now(), "event": verdict, "candidate_id": cand["_id"],
                                "version": version, "details": {"reasons": reasons}})
    return doc
