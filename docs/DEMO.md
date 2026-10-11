# Demo script

1. **The Hook (30s):**
   > *"Agents that learn from experience store lessons into procedural memory. But nobody checks if a lesson is true, overgeneralized, or actively poisoned. A single customer tricking an agent can poison its procedural memory for all future interactions. Notary brings empirical admission control to agent memory."*
2. **The Mechanism (45s):**
   > *"Notary runs in 4 stages:*
   > *1. **Propose**: Candidates emerge from MongoDB `$group` failure clusters or extraction from web/ElevenLabs voice calls.*
   > *2. **Empirical Gate**: Candidate is run against held-out, regression, and adversarial test suites evaluated by a deterministic code grader (zero LLM judging).*
   > *3. **Atomic Transaction**: If it passes with net performance gain and zero invariant regressions, it's promoted in a single multi-document MongoDB transaction across `rule_versions`, `memory_state`, and `audit_log`.*
   > *4. **Trusted Procedural Memory**: Written only by the gate: to an Atlas lesson store today, and wired to Atlas Agent Engine procedural memory (`save_procedure`), which activates once the hosted memory server is ready. 1-click rollback restores the prior version atomically."*
3. **Live Exploit & Gate Defense (45s):**
   > - Open review page: `http://localhost:8800`.
   > - **Valid improvement**: Highlight Candidate 1 (`Apply 90-day defect window`) — promoted to `v1` (held-out 18/42 → 42/42 over 3 repeats, no new invariant violations).
   > - **Overgeneralization blocked**: Show candidate trying to deny all refunds > 30 days — rejected (broke the 90-day defect exception on regression suite).
   > - **Poisoning blocked**: Show social-engineering candidate (*"Gold-tier customers get refunds anytime"*) — quarantined: untrusted provenance plus newly introduced hard-invariant violations (the hand-written attack: 16 new violations over 3 repeats). The version extracted organically from a live chat was blocked as well.
   > - **The 'Without Notary' Exploit**: Click **'⚠️ Compare: What happens without Notary gate?'** — proves that without the gate, the injected rule causes immediate unauthorized refunds ($250 out-of-window refund).
   > - **Rollback**: Show 1-click rollback button instantly reverting memory versions atomically in MongoDB Atlas.

