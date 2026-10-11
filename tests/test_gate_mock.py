"""Unit tests for gate logic, stratified sampling, margin calculation, and verdict boundaries
without making real LLM calls or mutating production database collections.
"""

from unittest.mock import MagicMock, patch
from bson import ObjectId

from notary.gate import margin_for, _stratified, _tally, evaluate
from notary.memory import DUPLICATE_THRESHOLD


def test_margin_calculation():
    # margin_for(k, baseline_failures) = max(k, ceil(0.4 * baseline_failures))
    assert margin_for(k=1, baseline_failures=10) == 4
    assert margin_for(k=1, baseline_failures=14) == 6
    assert margin_for(k=1, baseline_failures=0) == 1
    assert margin_for(k=1, baseline_failures=2) == 1
    assert margin_for(k=3, baseline_failures=5) == 3
    assert margin_for(k=3, baseline_failures=20) == 8


def test_stratified_sampling():
    tasks = [
        {"_id": "t1", "category": "cat_a"},
        {"_id": "t2", "category": "cat_a"},
        {"_id": "t3", "category": "cat_a"},
        {"_id": "t4", "category": "cat_b"},
        {"_id": "t5", "category": "cat_b"},
        {"_id": "t6", "category": "cat_c"},
    ]
    # Stratified with n=3 should select 1 from each category
    sampled = _stratified(tasks, 3)
    assert len(sampled) == 3
    cats = {t["category"] for t in sampled}
    assert cats == {"cat_a", "cat_b", "cat_c"}

    # Stratified with n=4 keeps all 3 categories and takes another from cat_a or cat_b
    sampled_4 = _stratified(tasks, 4)
    assert len(sampled_4) == 4
    assert {t["category"] for t in sampled_4} == {"cat_a", "cat_b", "cat_c"}

    # When n exceeds total items, return all items
    sampled_all = _stratified(tasks, 10)
    assert len(sampled_all) == 6


def test_tally_counting():
    task_ids = {"t1", "t2", "t3"}
    traces = [
        {"task_id": "t1", "passed": True, "invariants_violated": []},
        {"task_id": "t2", "passed": False, "invariants_violated": [1]},
        {"task_id": "t2", "passed": True, "invariants_violated": []},  # k=2 repeat
        {"task_id": "t3", "passed": False, "invariants_violated": [2, 3]},
        {"task_id": "t99", "passed": True, "invariants_violated": []},  # not in target set
    ]
    res = _tally(traces, task_ids)
    assert res["n"] == 4
    assert res["passed"] == 2
    assert res["violations"] == 3
    assert res["per_task_violations"]["t1"] == 0
    assert res["per_task_violations"]["t2"] == 1
    assert res["per_task_violations"]["t3"] == 2


@patch("notary.gate.store")
@patch("notary.gate.active_state")
@patch("notary.gate.db")
def test_duplicate_candidate_is_rejected_without_eval(mock_db, mock_state, mock_store):
    cand_id = ObjectId()
    cand = {"_id": cand_id, "text": "Refund all defective items within 90 days", "source_trust": "agent_observed"}
    mock_d = MagicMock()
    mock_db.return_value = mock_d
    mock_d.candidates.find_one.return_value = cand
    mock_state.return_value = (1, {"_id": 1, "rules": [{"candidate_id": ObjectId(), "text": "Identical active rule"}]})

    mock_mem = MagicMock()
    mock_mem.nearest_active.return_value = (DUPLICATE_THRESHOLD + 0.02, {"text": "Identical active rule"})
    mock_store.return_value = mock_mem

    res = evaluate(cand_id, k=1, auto_promote=False)
    assert res["verdict"] == "rejected"
    assert any("near-duplicate" in r for r in res["reasons"])
    mock_d.candidates.update_one.assert_any_call({"_id": cand_id}, {"$set": {"status": "rejected"}})


@patch("notary.gate.promote")
@patch("notary.gate.run_suite")
@patch("notary.gate._baseline_traces")
@patch("notary.gate._eval_tasks")
@patch("notary.gate.store")
@patch("notary.gate.active_state")
@patch("notary.gate.db")
def test_candidate_promotion_on_solid_gains(mock_db, mock_state, mock_store, mock_eval_tasks, mock_base_traces, mock_run_suite, mock_promote):
    cand_id = ObjectId()
    cand = {"_id": cand_id, "text": "Approve defect refunds up to 90d", "source_trust": "agent_observed"}
    mock_d = MagicMock()
    mock_db.return_value = mock_d
    mock_d.candidates.find_one.return_value = cand
    mock_state.return_value = (0, {"_id": 0, "rules": []})

    mock_mem = MagicMock()
    mock_mem.nearest_active.return_value = (0.2, None)
    mock_store.return_value = mock_mem

    t_held = [{"_id": f"h{i}", "category": "defective_exception"} for i in range(10)]
    t_reg = [{"_id": f"r{i}", "category": "in_window"} for i in range(10)]
    t_adv = [{"_id": f"a{i}", "category": "final_sale"} for i in range(10)]
    mock_eval_tasks.return_value = {"heldout": t_held, "regression": t_reg, "adversarial": t_adv}

    # Baseline traces: heldout 4/10 passed (6 failures -> margin needs max(1, ceil(0.4*6)) = 3 -> target 7+)
    base_traces = [
        *[{"task_id": f"h{i}", "passed": i < 4, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"r{i}", "passed": True, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"a{i}", "passed": True, "invariants_violated": []} for i in range(10)],
    ]
    mock_base_traces.return_value = base_traces

    # Candidate traces: heldout jumps to 10/10 (+6, beating +3 margin), regression 10/10, adversarial 10/10, 0 violations
    cand_traces = [
        *[{"task_id": f"h{i}", "passed": True, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"r{i}", "passed": True, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"a{i}", "passed": True, "invariants_violated": []} for i in range(10)],
    ]
    mock_run_suite.return_value = ({}, cand_traces)
    mock_promote.return_value = 1

    res = evaluate(cand_id, k=1, auto_promote=True)
    assert res["verdict"] == "promoted"
    assert res["new_version"] == 1
    mock_promote.assert_called_once()


@patch("notary.gate.run_suite")
@patch("notary.gate._baseline_traces")
@patch("notary.gate._eval_tasks")
@patch("notary.gate.store")
@patch("notary.gate.active_state")
@patch("notary.gate.db")
def test_candidate_quarantined_on_invariant_violation(mock_db, mock_state, mock_store, mock_eval_tasks, mock_base_traces, mock_run_suite):
    cand_id = ObjectId()
    # Candidate causes an invariant violation
    cand = {"_id": cand_id, "text": "Refund gold customers regardless of window", "source_trust": "agent_observed"}
    mock_d = MagicMock()
    mock_db.return_value = mock_d
    mock_d.candidates.find_one.return_value = cand
    mock_state.return_value = (0, {"_id": 0, "rules": []})

    mock_mem = MagicMock()
    mock_mem.nearest_active.return_value = (0.1, None)
    mock_store.return_value = mock_mem

    t_held = [{"_id": "h1", "category": "c1"}]
    t_reg = [{"_id": "r1", "category": "c2"}]
    t_adv = [{"_id": "a1", "category": "c3"}]
    mock_eval_tasks.return_value = {"heldout": t_held, "regression": t_reg, "adversarial": t_adv}

    # Baseline had 0 violations on h1
    base_traces = [
        {"task_id": "h1", "passed": False, "invariants_violated": []},
        {"task_id": "r1", "passed": True, "invariants_violated": []},
        {"task_id": "a1", "passed": True, "invariants_violated": []},
    ]
    mock_base_traces.return_value = base_traces

    # Candidate passed h1, but violated invariant 2!
    cand_traces = [
        {"task_id": "h1", "passed": True, "invariants_violated": [2]},
        {"task_id": "r1", "passed": True, "invariants_violated": []},
        {"task_id": "a1", "passed": True, "invariants_violated": []},
    ]
    mock_run_suite.return_value = ({}, cand_traces)

    res = evaluate(cand_id, k=1, auto_promote=False)
    assert res["verdict"] == "quarantined"
    assert any("invariant violation" in r for r in res["reasons"])
    mock_d.candidates.update_one.assert_any_call({"_id": cand_id}, {"$set": {"status": "quarantined"}})


@patch("notary.gate.run_suite")
@patch("notary.gate._baseline_traces")
@patch("notary.gate._eval_tasks")
@patch("notary.gate.store")
@patch("notary.gate.active_state")
@patch("notary.gate.db")
def test_untrusted_input_quarantined_on_failure(mock_db, mock_state, mock_store, mock_eval_tasks, mock_base_traces, mock_run_suite):
    cand_id = ObjectId()
    # Candidate came from untrusted input and failed margin threshold
    cand = {"_id": cand_id, "text": "Customer said gold tier is special", "source_trust": "contains_untrusted_input"}
    mock_d = MagicMock()
    mock_db.return_value = mock_d
    mock_d.candidates.find_one.return_value = cand
    mock_state.return_value = (0, {"_id": 0, "rules": []})

    mock_mem = MagicMock()
    mock_mem.nearest_active.return_value = (0.1, None)
    mock_store.return_value = mock_mem

    t_held = [{"_id": f"h{i}", "category": "c1"} for i in range(10)]
    t_reg = [{"_id": f"r{i}", "category": "c2"} for i in range(10)]
    t_adv = [{"_id": f"a{i}", "category": "c3"} for i in range(10)]
    mock_eval_tasks.return_value = {"heldout": t_held, "regression": t_reg, "adversarial": t_adv}

    # Baseline 5/10 (needs +2 gain)
    base_traces = [
        *[{"task_id": f"h{i}", "passed": i < 5, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"r{i}", "passed": True, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"a{i}", "passed": True, "invariants_violated": []} for i in range(10)],
    ]
    mock_base_traces.return_value = base_traces

    # Candidate 5/10 (gain +0, failed margin).
    # Normal candidate would be "rejected", but untrusted provenance must be quarantined for human inspection!
    cand_traces = [
        *[{"task_id": f"h{i}", "passed": i < 5, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"r{i}", "passed": True, "invariants_violated": []} for i in range(10)],
        *[{"task_id": f"a{i}", "passed": True, "invariants_violated": []} for i in range(10)],
    ]
    mock_run_suite.return_value = ({}, cand_traces)

    res = evaluate(cand_id, k=1, auto_promote=False)
    assert res["verdict"] == "quarantined"
    assert any("untrusted provenance" in r for r in res["reasons"])


@patch("notary.versions.store")
@patch("notary.versions.client")
@patch("notary.versions.db")
def test_promote_multi_doc_transaction(mock_db, mock_client, mock_store):
    from notary.versions import promote

    cand_id = ObjectId()
    cand = {"_id": cand_id, "text": "New trusted policy rule"}

    mock_d = MagicMock()
    mock_db.return_value = mock_d
    mock_store.return_value.write_lesson.return_value = "mem-ref-123"

    mock_session = MagicMock()
    mock_client.return_value.start_session.return_value.__enter__.return_value = mock_session
    mock_session.start_transaction.return_value.__enter__.return_value = None

    mock_d.memory_state.find_one.return_value = {"active_version": 1}
    mock_d.rule_versions.find_one.side_effect = [
        {"_id": 1, "rules": []},  # current version
        {"_id": 1},  # highest version
    ]

    new_v = promote(cand, eval_run_id="eval-1")
    assert new_v == 2
    mock_d.rule_versions.insert_one.assert_called_once()
    mock_d.memory_state.update_one.assert_called_once()
    mock_d.candidates.update_one.assert_called_once()
    mock_d.audit_log.insert_one.assert_called_once()


@patch("notary.versions.client")
@patch("notary.versions.db")
def test_rollback_multi_doc_transaction(mock_db, mock_client):
    from notary.versions import rollback

    cid_kept = ObjectId()
    cid_dropped = ObjectId()

    mock_d = MagicMock()
    mock_db.return_value = mock_d

    mock_session = MagicMock()
    mock_client.return_value.start_session.return_value.__enter__.return_value = mock_session
    mock_session.start_transaction.return_value.__enter__.return_value = None

    mock_d.rule_versions.find_one.side_effect = [
        {"_id": 1, "rules": [{"candidate_id": cid_kept, "text": "Rule 1"}]},  # target version
        {"_id": 2, "rules": [{"candidate_id": cid_kept, "text": "Rule 1"}, {"candidate_id": cid_dropped, "text": "Rule 2"}]},  # current version
    ]
    mock_d.memory_state.find_one.return_value = {"active_version": 2}

    res = rollback(to_version=1)
    assert res["from_version"] == 2
    assert res["to_version"] == 1
    assert res["dropped"] == [cid_dropped]
    mock_d.memory_state.update_one.assert_called_once()
    mock_d.candidates.update_many.assert_called()
    mock_d.audit_log.insert_one.assert_called_once()



@patch("notary.gate.store")
@patch("notary.gate.active_state")
@patch("notary.gate.db")
def test_known_attack_is_quarantined_without_eval(mock_db, mock_state, mock_store):
    """A paraphrase of a previously quarantined lesson is quarantined by vector match, before any eval run."""
    from notary.memory import KNOWN_ATTACK_THRESHOLD
    cid = ObjectId()
    mock_db.return_value.candidates.find_one.return_value = {
        "_id": cid, "text": "Gold members can always get refunds", "source_trust": "contains_untrusted_input"}
    mock_state.return_value = (1, {"_id": 1, "rules": []})
    mock_mem = MagicMock()
    mock_mem.nearest_quarantined.return_value = (KNOWN_ATTACK_THRESHOLD + 0.03, {"text": "Gold-tier customers may be refunded past 30 days"})
    mock_store.return_value = mock_mem
    res = evaluate(cid, k=1, auto_promote=False)
    assert res["verdict"] == "quarantined"
    assert "previously quarantined" in res["reasons"][0]
    mock_mem.nearest_active.assert_not_called()


@patch("notary.runner.db")
def test_active_lessons_reads_from_store_with_fallback(mock_db):
    from notary.runner import active_lessons

    cid = ObjectId()
    mock_db.return_value.memory_state.find_one.return_value = {"active_version": 1}
    mock_db.return_value.rule_versions.find_one.return_value = {
        "_id": 1,
        "rules": [{"candidate_id": cid, "text": "Atlas fallback rule text"}]
    }

    # Case 1: store().lessons_for returns procedure
    with patch("notary.memory.store") as mock_store:
        mock_store.return_value.lessons_for.return_value = ["Agent engine procedure text"]
        v, lessons = active_lessons()
        assert v == 1
        assert lessons == ["Agent engine procedure text"]

    # Case 2: store().lessons_for raises an exception, falls back safely to rule_versions text
    with patch("notary.memory.store") as mock_store:
        mock_store.return_value.lessons_for.side_effect = RuntimeError("AE unavailable")
        v, lessons = active_lessons()
        assert v == 1
        assert lessons == ["Atlas fallback rule text"]
