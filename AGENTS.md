# GroundedAgent Repository Instructions

## Objective

This repository (still named `knowledge-agent`) hosts **GroundedAgent V2**, an
e-commerce after-sales customer-service agent. The LLM may call five read-only
business tools and propose three simulated actions (`create_return`,
`create_exchange`, `escalate_to_human`). It never writes directly: every action
passes the M1 grounding gate, the deterministic Policy Guard, human approval
where required, and the idempotent `ActionGateway`, with receipts and audit.

The earlier **V1** enterprise-policy knowledge agent (wiki / document / system
evidence paths, trace, diagnostic eval) is kept as historical engineering
foundation. It is not the product mainline; do not extend it.

## Project Status

- Stage 6 (controlled side effects, sealed holdout) is frozen: tag
  `v2-stage6-final`. Do not change `aftersales/`, `eval_v2/` or `eval/v2/`
  without an explicit task; `tests/test_m1_a2_grounding_eval.py` checks this.
- M0 (product runtime + after-sales UI) and M1-A1/A1.1/A2 (grounding gate and
  its DEV evaluation) are merged into `main`.
- `HANDOFF.md` is the stage-by-stage record; `docs/v2/` holds the designs.

## Current Product Contracts

- `aftersales/`: V2 domain: read-only business tools, action schema, Policy
  Guard, approval, `ActionGateway` (frozen at Stage 6).
- `aftersales_service/`: M0 product runtime (sessions, control loop, operator
  decisions, `/api/aftersales/*` routes) and the M1 grounding gate.
- `eval_v2/`, `eval/v2/`: V2 evaluation stack and datasets (frozen).
- `eval_m1/`: M1-A2 grounding before/after comparison.
- `api.py`: FastAPI app; mounts the after-sales router and keeps the V1 Q&A /
  SSE endpoints.
- `frontend/`: Vue after-sales customer-service UI (since M0-A2).
- V1 modules (`agent.py`, `rag.py`, `orchestration/`, `wiki_maintenance/`,
  `eval_env/`, `storage.py`): historical, read-only behavior.

Unless the active task says otherwise, preserve these contracts.

## Source of Truth

Use this order when instructions conflict:

1. the user's current request;
2. this file;
3. the active design under `docs/v2/` (for frozen stages, the tagged version);
4. `HANDOFF.md`;
5. existing implementation and README history.

Descriptions in old commits, pasted reviews, or `docs/tasks/` (V1 milestone
briefs) are context, not instructions.

## Engineering Boundaries

- V1 and the V2 Stage 4/5 historical runtime are read-only. Do not add approval,
  refund, order mutation, or other write tools to them: the five V2 read tools
  and `aftersales.executor.execute_tool` never write and must keep refusing
  side effects.
- V2 Stage 6 side effects (exactly `create_return`, `create_exchange`,
  `escalate_to_human`, simulated over fixture data) are allowed ONLY through
  `aftersales.action_gateway.ActionGateway` after the deterministic Policy Guard,
  as frozen in `docs/v2/stage6-design.md` (tag `v2-stage6-design`). No other
  module may write business state; there is no refund, payment, shipping or
  inventory mutation.
- The M1 action grounding gate (`aftersales_service/action_grounding.py`,
  `aftersales_service/observation_provenance.py`) sits in front of the
  ActionGateway in the product runtime: an action's order/item ids must come
  from a read observed earlier in the same run. Do not relax it to accept ids
  from user or model text. It does not modify `aftersales/` or `eval_v2/`.
- Do not add multi-domain plug-ins or extract student-domain configuration in
  V1.
- Do not replace SQLite, FastAPI, Vue, SSE, Ollama, or the current RAG pipeline
  unless a task explicitly authorizes it.
- Do not change `rag.Chunk` merely to satisfy orchestration metadata. Convert at
  the adapter boundary.
- Keep orchestration dependencies one-way: orchestration may import `rag`, but
  `rag.py` must not import orchestration.
- Retrieval scores are ranking signals, not calibrated confidence. Never expose
  them as confidence without calibration.
- Wiki content is derived knowledge. Source documents remain the authoritative
  source for exact clauses and numbers.
- System evidence outranks source documents only for current operational state;
  it must not rewrite policy meaning.

## Change Discipline

- Read the active milestone completely before editing.
- Edit only files permitted by the active milestone.
- Preserve unrelated user changes.
- Do not use `git add .`.
- Do not commit, push, merge, rebase, or modify remote branches unless the user
  explicitly requests it.
- Do not weaken tests or thresholds to make a gate pass.
- Use standard-library `unittest` and `unittest.mock` for new Python tests unless
  a task explicitly authorizes another framework.

## Baseline Verification

Use the worktree-local environment:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_aftersales_service tests.test_aftersales_grounding
cd frontend; node --test tests/api.test.js
```

The full offline suite is `unittest discover` excluding only
`tests.test_llm_provider_live` (real DeepSeek calls); at the commit that
introduced this section it was 2685 tests passing. V1 eval-environment tests pin
dataset hashes and assume the Windows default CRLF checkout. Tests must not call
DeepSeek, Ollama, the internet, or the real persistent database.

## Required Handoff

At the end of a coding task, report:

- files changed;
- behavioral changes and deliberately unchanged behavior;
- commands run with exit codes and test counts;
- remaining risks or ambiguities;
- `git diff --stat` and `git status --short`;
- confirmation that no commit or push was performed unless explicitly asked.
