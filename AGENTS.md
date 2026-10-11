# AGENTS.md — context for coding agents (Antigravity, Claude Code, Cursor, …)

Read this and [README.md](README.md) first. The design spec is [docs/SPEC.md](docs/SPEC.md) (sections marked
**[v3]** win over older text). Two agents are working on this repo at the same time — see "Coordination".

## What this is
**Notary**: an evaluation gate in front of an AI agent's procedural memory. Lessons (proposed from failures or
extracted from customer conversations) only become trusted memory if they pass held-out / regression / adversarial
suites with no newly introduced hard-invariant violations. Promotion and rollback are MongoDB transactions with an
audit log. Domain: a synthetic refund desk "Acme". Hackathon: MongoDB #SFTechWeek, Statement One (Recursive Harnessing).

## Hard rules (do not break)
1. **Never commit `.env`** or print secrets (MONGODB_URI, OPENAI_API_KEY, VOYAGE_API_KEY, ELEVENLABS_API_KEY,
   AE_CLIENT_SECRET, NOTARY_VOICE_TOKEN). `.env` is gitignored; add new names to `.env.example` with empty values.
2. **All persistence goes to the organizer's Atlas Sandbox** (`MONGODB_URI`, db `notary`). No other database.
3. **The trusted policy lives only in `notary/policy.py`.** Never put the full policy into the agent prompt or
   `POLICY_SUMMARY` — the agent must have something to learn.
4. **Grading is pure code** (`notary/grader.py`). Do not add LLM judging to the gate.
5. **Do not tune the gate or the prompts to make a demo pass.** Thresholds in `notary/gate.py` are fixed rules; if
   you change one, say so in the commit message and README. Never weaken the baseline agent to inflate gains.
6. **Never show held-out / regression / adversarial data to the proposer.** Refinement uses the TRAIN split only.
7. **The gate is the only writer to trusted procedural memory** (`versions.promote` → `memory.store().write_lesson`).
8. Report counts out of N×k, never bare percentages. Disclose any manually inserted candidate (`created_by=manual_attack`).
9. No Streamlit; the web page must not become dashboard-first (hackathon banned list).
10. Commit small and often with clear messages; the commit history is the "built today" evidence.

## Layout
| Path | What |
|---|---|
| `notary/policy.py` | trusted refund policy, hard invariants 1–4, deliberately incomplete `POLICY_SUMMARY` |
| `notary/taskgen.py` | seeded generator: customers, orders, tasks per split (`SPLIT_PLAN`), `TODAY = 2026-10-11` |
| `notary/grader.py` | deterministic grading, failure categories, invariant 5 (followed injection), `FAILURE_TARGETS` |
| `notary/llm.py` | provider-neutral tool loop (OpenAI/Anthropic), `complete()`; models via env (`LLM_MODEL`, `PROPOSER_MODEL`) |
| `notary/agent.py` | the refund agent: `agent_turn(message, lessons)` — the ONE brain used by gate, web chat and voice |
| `notary/runner.py` | parallel suite runner → `traces`; `active_lessons()` |
| `notary/proposer.py` | failure clustering (aggregation), proposer w/ contrast set, `refine_on_train`, `extract_from_call` |
| `notary/gate.py` | `evaluate(candidate_id, k, quick)` → promoted / rejected / quarantined; baseline cache `base-v{N}-k{k}` |
| `notary/versions.py` | `promote()` / `rollback()` in multi-document transactions + audit log |
| `notary/memory.py` | `AtlasVoyageStore` (Atlas `lessons` + Voyage embeddings) and `AgentEngineStore` (Agent Engine procedural memory, fail-safe) |
| `notary/ae_auth.py` | Agent Engine service-account token exchange |
| `notary/server.py` | FastAPI: review page API, `/api/chat`, extract-and-gate jobs, ungated comparison, rollback, **voice** `/v1/chat/completions` (SSE, token `X-Notary-Token`), tunnel guard |
| `notary/static/index.html` | single-page UI (vanilla JS) |
| `notary/cli.py` | `check seed baseline clusters learn refine add gate rollback report` |
| `agentengine/project-config.yaml` | Agent Engine memory-only project; extraction enabled = semantic, episodic (NOT procedural) |
| `scripts/voice_agent.py` | create / re-point the ElevenLabs agent at a tunnel URL |
| `tests/test_core.py` | unit tests for policy, generator, grader — run `.venv/bin/python -m pytest -q` |

## MongoDB collections (db `notary`)
`customers, orders, tasks` (seeded) · `traces` (every agent run; run_id prefixes: `base-` baseline cache, `cand-`
candidate eval, `draft-` refinement, `ungated-` comparison) · `candidates` (status: proposed / evaluating / promoted /
rejected / quarantined / rolled_back) · `eval_runs` · `rule_versions` (`_id` = version int) · `memory_state`
(`_id: "state"`, `active_version`) · `audit_log` (append-only) · `lessons` (Atlas mirror + embedding) · `calls`
(web chat + voice transcripts) · `ungated_memory`.

## Environment
Python 3.13 venv at `.venv`. Models: agent `gpt-4.1`, proposer `gpt-5.5`, extractor `gpt-4.1-mini`
(`LLM_PROVIDER=openai` is pinned because the host environment may carry an unrelated ANTHROPIC_API_KEY).
Voyage base URL `https://us.ai.mongodb.com/v1` (the key is US-scoped). Server: `.venv/bin/uvicorn notary.server:app --port 8800`.
A full gate run (k=1) takes ~1–1.5 min and ~150 agent calls; `--quick` ~30 s.

## Current status (update this when you change things)
- ✅ Core loop works end to end; verdicts verified (see README results table).
- ✅ Review page + chat + ungated comparison; voice endpoint tested; ElevenLabs agent created (talk link via `scripts/voice_agent.py`).
- ⏳ Agent Engine memory server: provisioning was blocked by a rejected Voyage key; promotion falls back to Atlas
  automatically until it is ready (check `agentengine memory status --context notary`).
- ⏳ Not yet done: k=3 reruns for final numbers; re-run the organic gold-tier poisoning under the untrusted rule;
  real voice call test; Raindrop signals (optional); deploy on Modelence (optional).

## Good tasks for a second agent (low conflict)
- **UI polish** in `notary/static/index.html` only: clearer verdict cards, a before/after diff view, a short
  "how it works" strip, responsive layout. Keep it a page about the gate's decisions, not a dashboard.
- **More tests** in `tests/`: gate verdict logic with fake traces (no LLM calls), `_stratified`, `margin_for`,
  versions promote/rollback against a test database name.
- **README**: architecture diagram (Mermaid), demo script, screenshots.
- Avoid editing `gate.py`, `proposer.py`, `memory.py`, `server.py` without coordinating — those are being changed now.

## Coordination
Claude Code is also working in this repo. To avoid clobbering each other: pull before you start
(`git pull --rebase`), keep commits small, commit often, and prefer working on a branch (`git switch -c antigravity/<topic>`)
then merging. Don't reformat files you aren't changing. Don't reseed or clear the database without saying so —
it wipes the recorded verdicts.
