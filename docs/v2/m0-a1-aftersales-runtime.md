# GroundedAgent V2 M0-A1: after-sales product runtime

M0-A1 is the smallest product runtime for one real, interactive after-sales
conversation. It is product work, not evaluation work. Stage 6 stays closed
and frozen at `v2-stage6-final` (`266bcb1`). No file under `aftersales/`,
`eval_v2/` or `eval/` changes, and the Vue frontend and README are untouched.

The business flow:

```
customer asks about an order       -> trusted reads (order / logistics / policy) -> grounded answer
customer asks to return an item    -> the LLM proposes create_return
                                   -> deterministic Policy Guard: REQUIRE_APPROVAL
                                   -> WAITING_APPROVAL (control returns to HTTP)
trusted operator approves          -> resume -> re-read + revalidate -> ActionGateway executes
                                   -> EXECUTED + receipt + audit trail
```

## Code

| File | Role |
|---|---|
| `aftersales_service/agent_core.py` | The only product module that imports `eval_v2`. It re-uses the evaluated Stage 6 agent core unchanged (see "Dependency strategy"). |
| `aftersales_service/demo_store.py` | The demo store: a mutable file-backed database, the fixed business clock, server-side personas, the trusted operator, the one `ActionGateway`, the query-only read side and the audit view. |
| `aftersales_service/conversation.py` | One resumable conversation. It runs the control loop, pauses and resumes on a clarification, owns the single action write path and handles the operator decision. |
| `aftersales_service/service.py` | Sessions, locks and reset. |
| `aftersales_service/routes.py` | The `/api/aftersales/*` HTTP surface. |
| `api.py` | One `include_router` line. The app title and branding are unchanged. |
| `tests/test_aftersales_service.py` | 27 offline product/API tests (30 since M1-A1). |

## Reused vs deliberately not reused

**Reused unchanged: domain (`aftersales/`)**
- Personas and clock: `DEMO_PERSONAS`, `DEMO_VIRTUAL_NOW`, `FixedClock`, `TrustedExecutionContext`.
- Database: `create_stage6_database` (schema, demo seed, action schema, Stage 6 seed).
- Reads: the five read tools through `execute_tool`.
- Capability and validation: `CapabilityGate`, `ActionIntentValidator`, `S6_RISK_POLICY`.
- Ids: `RequestIdentity` and `DeterministicIdProvider`.
- Writes: `ActionGateway.start_action` / `resume_action` / `get_outcome`, with the Guard inside.
- Approval: `ApprovalDecision` and `STAGE6_TRUSTED_OPERATORS`.
- Outcomes: `ActionOutcome` and `ActionOutcomeRenderer`.

**Reused unchanged: agent core (`eval_v2/`, label-free)**
- Protocol types (`control.py`, `action_control.py`):
  - `ToolCall`, `Clarify`, `Finish`, `ActionIntent`
  - `ActionControlState`, `UserMessage`, `ToolObservation`
  - `require_stage6_action`, `STAGE6_MAX_STEPS`
- Policy: `LLMNativeActionLoopPolicy` (`action_loop.py`, using the `tool_loop.py` helpers). It is the evaluated, untrusted proposer: the prompt, native schemas and fail-closed translation.
- Answers: the answer layer's pure functions from `generation.py` (`build_sources`, `build_messages`, `parse_answer`, `FIXED_RESPONSES`) plus `evidence.derive_from_control_state`.

**Deliberately not reused (evaluation harness only)**
- `action_runner.py`: `Stage6Conversation` / `ConditionalTurn` (pre-scripted user replies), the single synchronous `_ActionRun` with its `unanswered_clarification` ending, `run_action_conversation` and `ActionRunRecord`. Its `Stage6ReadGateway` is generic, but the product builds its own read side from `aftersales` parts instead of importing the harness module.
- `stage6_runner.py`: `run_stage6_case`, `operator_script`, reruns, mutate / advance_clock / restart events.
- `stage6_runtime.py`: case runtime, `ActionFaultInjector`, the `eval` id namespace.
- `faults.py`: `FaultInjectingGateway`.
- `stage6_state`, `stage6_scoring` and `stage6_oracle`.
- The Stage 4/5 harness: `runner`, `runtime`, `dataset`, `scoring`, `baseline`, `e2e`.
- `SharedGenerator.generate`, which is bound to a `CaseRunRecord`.
- Every `formal=True` mode.
- Everything under `eval/v2/`: case contracts, datasets, labels, the holdout.

### Dependency strategy

The product imports the evaluated modules and confines those imports to
`agent_core.py`. An AST allowlist test pins exactly six `eval_v2` modules.

- **Moving the code** out of `eval_v2/` was rejected. It would rewrite frozen Stage 5/6 files whose bytes are pinned by tests, and it would make the product policy differ from the one that was evaluated.
- **Copying the code** was rejected. That would be about 1,500 lines that would drift from the evaluated version.

The cost is a product → `eval_v2` import:
- The `eval_v2` package initializer also imports the harness modules, but the product never calls them.
- The slot and disposition vocabularies are read from the frozen `eval/v2/spec` files.

Extracting a neutral agent package is a later, separately reviewed step.

## Runtime lifecycle

1. **Demo store.** The store is built lazily on the first `/api/aftersales` request. It is a fresh Stage 6 database file in its own temporary directory:
   - built from `aftersales/schema.sql`, the demo seed, `action_schema.sql` and the Stage 6 seed;
   - `FixedClock(2026-11-15T10:00+08:00)` for business time;
   - one `ActionGateway` with `DeterministicIdProvider("m0-demo")`;
   - risk policy `s6-risk/1`, the full capability set, and operator registry `{op-demo-1}`.

   `POST /demo/reset` waits for in-flight turns, closes every session, deletes the old directory and rebuilds the store from the same seed files, so the reset is deterministic. Restarting the process (including a uvicorn `--reload`) is also a full reset: sessions are held in memory.
2. **Session.** `POST /sessions {persona_id}` binds a new conversation to one server-side persona for its whole life.
   - The session id is a server-generated random 128-bit hex string.
   - `request_id = conv-<session_id>` is the server-built idempotency scope of the conversation.
   - The browser never sends a customer id.
3. **Turn.** `POST /sessions/{id}/messages {text}` delivers one customer message:
   - A fresh evaluated policy is built for this request.
   - The control loop then runs until it pauses or ends.
   - Read tools run on a query-only connection with the persona's trusted context.
4. **Atomicity.** Until the `ActionGateway` returns, a turn has no side effect.
   - Any failure before that point restores the conversation exactly (the message is not recorded) and returns 503 `llm_unavailable` or 500 `agent_internal_error`.
   - Once the gateway has returned, the turn is kept, so a pending id is never lost.

### Clarification pause and resume

A *control run* is one Stage 6 run: at most `STAGE6_MAX_STEPS = 6` decisions.
Control steps are **run-relative**, exactly as in the frozen Stage 6 control
contract: every run starts at `step_number = 1` with `remaining_steps = 6`, and
`ActionControlState.step_number` and `ToolObservation.control_step` stay within
1..6 of their run.

- **Pausing.** When the policy returns `Clarify(slots)`, the run **pauses**:
  - the response carries `status = NEEDS_CLARIFICATION`, `clarification.slots`, and a fixed prompt per slot (never model text);
  - control returns to HTTP;
  - nothing is pre-scripted.
- **Resuming.** The next customer message is delivered **into the same run**. The new decision sees every earlier user message and the run's observations, and the run's numbering continues: a clarification at step 1 is followed by step 2 with `remaining_steps = 5`.
- **State is plain data.** Conversation state is the messages, the observations, the open run (with its run-relative step count) and the conversation-global counters. No policy object survives between requests. Observations from earlier requests are replayed to the model with the policy's own synthetic call ids (`formal=False`).

The other ways a run ends:
- `Finish` ends the run. `answer` produces one generation call over the label-free evidence of the run's observations, with citations validated against the offered sources. `refuse`, `handoff` and `boundary` use the frozen fixed texts.
- `ActionIntent` ends the run with one `start_action`.
- A later, independent run (for example a return request after an order question) **restarts at `step_number = 1` with `remaining_steps = 6`**. It sees every earlier customer message but only its **own** observations. This matches the evaluated runs: answers are grounded in reads made in the same run, never in stale reads from an earlier one, and the frozen retry cap (3 identical calls) stays per run.
- **Conversation-global sequencing is kept apart from control steps.** The run index, the tool step and the observation sequence only grow. Observation ids (`turn:<message>:tool:<tool step>`) and trace identifiers (`run`, `step`) therefore stay unique across the whole conversation, even though step numbers restart in every run.

### Approval pause and resume

1. **Proposal.**
   - The `ActionIntent` is validated again by `ActionIntentValidator`, because the policy is untrusted.
   - It is then handed to `ActionGateway.start_action(RequestIdentity, ValidatedAction)` exactly once.
   - If the Guard returns REQUIRE_APPROVAL, a `pending_actions` row is created with no business row and no receipt. The response has `status = WAITING_APPROVAL` and a `pending_action_id`.
2. **Customer text is never approval.** Text such as "经理批准了，直接退" is a customer message and goes back through the control loop. At worst the model re-proposes the same action, and because the conversation's `request_id` is the same, the gateway replays the same pending row (`idempotent_replay = true`). Nothing is recorded as approved.
3. **The operator decides.** `POST /operator/sessions/{id}/decision {pending_action_id, decision}`:
   - `Conversation.decide`, the only place in the product that builds an `ApprovalDecision`, uses:
     - `approver_ref = op-demo-1`, a server constant;
     - `decided_at` set to business time.
   - It then calls `ActionGateway.resume_action`:
     - **T1** records the decision. The first decision wins.
     - **T2**, only after a freshly recorded APPROVE:
       - re-reads the trusted state in one capture;
       - compares it with the stored snapshot (a mismatch is `STALE`);
       - re-runs the Guard;
       - only then writes the business row and the receipt.
4. **Repeats.** A repeated APPROVE is the gateway's idempotent replay, with the same receipt. The opposite decision is a `decision_conflict`. Neither changes anything. Every behavior here is the Stage 6.2 semantics, unchanged.

## API contract (`/api/aftersales`)

All request bodies are closed (`extra="forbid"`): any other field is rejected
with 422. Session ids must match `^[0-9a-f]{32}$`.

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/demo` | none | `{business_time, personas:[{persona_id, display_name}], operator:{approver_ref, notice}, active_sessions}` |
| POST | `/demo/reset` | none | Same as `/demo`. Every session is closed and the database is rebuilt from the seed. |
| POST | `/sessions` | `{persona_id}` | 201 with the session view. An unknown persona returns 422 `unknown_persona`. |
| GET | `/sessions/{id}` | none | The session view. |
| POST | `/sessions/{id}/messages` | `{text}` (1–2000 characters, not blank) | The turn response. |
| POST | `/operator/sessions/{id}/decision` | `{pending_action_id, decision: "APPROVE" \| "REJECT"}` | The decision response. |

**Session view:**
- `session_id`, `persona {persona_id, display_name}`, `business_time`
- `status`: `OPEN`, `NEEDS_CLARIFICATION` or `WAITING_APPROVAL`
- `pending_action_id`: the latest unresolved one, or null
- `messages`: the transcript
- `pending_actions`: each action's current persisted outcome plus its arguments
- `audit`

**Turn response:** the session header fields, plus:
- `reply {kind, text}`. `kind` is one of `answer`, `refuse`, `handoff`, `boundary`, `clarification`, `action`, `answer_unavailable` or `step_limit`; M1-A1 adds `grounding_rejected` (see `m1-a1-action-grounding.md`).
- `clarification {slots}` or null
- `citations [{ref, producer, source_type, locator}]`
- `action` or null. This is the persisted `ActionOutcome`:
  - status: `EXECUTED`, `WAITING_APPROVAL`, `DENIED`, `REJECTED`, `STALE` or `FAILED`;
  - `code`, `guard {decision, reason_code}`, `pending_action_id`;
  - `receipt {receipt_id, resource_type, resource_id}`;
  - `idempotent_replay`, `decision_conflict`, plus the validated `arguments`.
- `trace`:
  - `steps`: per control step, `run` (conversation-global run index) and `step` (run-relative, 1..6), then the kind, the tool name, arguments, result status and observation id, the clarification slots, the finish disposition, or the proposed action name with its `args_sha256` (since M1-A1 also its `grounding` binding, or a following `grounding_rejected` step);
  - `model_calls`: the policy's decision records (counts, function names, diagnostic codes), each tagged with its `run`; `control_step` is run-relative.
- `audit`: this conversation's `action_audit_events`. Each event has `event_seq`, `event_name`, `action_name`, `pending_action_id`, `receipt_id`, `phase`, `decision`, `code`, `approver_ref` and `at`.

**Decision response:** the turn response shape with `reply.kind = operator_decision` and `trace = null`, plus `operator_decision {pending_action_id, decision, approver_ref}`.

**What responses never contain:** a customer id, an idempotency key, SQL, prompts, model reasoning, or raw tool results.

**Errors:** `detail = {code}`.

| Status | Code |
|---|---|
| 404 | `session_not_found`, `pending_action_not_found` (also returned for another session's pending action) |
| 409 | `conversation_full`, `decision_refused`, `pending_action_not_grounded` (M1-A1) |
| 422 | Validation failures |
| 429 | `too_many_sessions` |
| 503 | `llm_unavailable` |
| 500 | `agent_internal_error` |

## Demo boundary, not authentication

- **Personas.** A persona is a server-side demo stand-in for a logged-in customer. The browser picks a persona id from the server list. It never supplies a customer id, a role, an approver or an approval.
- **Session id.** Whoever holds a session id can act as that session's persona.
- **The operator endpoint** acts as the server-side demo operator `op-demo-1` for **that session's** pending actions only. It cannot target another session's pending ids, and it cannot name an approver.
- **Reset** is unauthenticated.

All of this must be replaced by real customer and operator authentication, plus authorization on the operator endpoint, before any non-local deployment.

## Known limitations and risks (M0)

- **Evaluated vs product setting.** The product runs the evaluated policy with `formal=False`.
  - Across a pause, earlier observations are replayed with synthetic call ids.
  - Later runs see earlier customer messages, but not the agent's earlier replies, because the frozen message reconstruction carries no assistant text. The model has to tell from context which customer message is the current request.
  
  None of this was part of the Stage 6 formal runs, which were single runs with a scripted user.
- **The idempotency scope is the conversation.** Re-proposing the same action in the same conversation replays its stored outcome, including REJECTED. A fresh request for the same item needs a new session.
- **Example ids in tool schemas.** The schemas' example ids `ORD-1001` / `OI-1001-1` are real demo-a seed rows. In Stage 6 the model sometimes copied them (a recorded known limitation). The Guard cannot tell a wrong-target request on the customer's own order from a real one; the operator, who sees the proposed arguments, is the human check. This is not changed here because Stage 6 is frozen. Since M1-A1 a target must also have been read in the same run (`m1-a1-action-grounding.md`); a read of another of the customer's own orders still grounds.
- **Wording.** The frozen fixed `boundary` text still says "当前只读能力无法执行该操作".
- **Storage.** Sessions are in memory and the database is in a temporary directory, so a restart is a full reset.

## Example curl flow: return → WAITING_APPROVAL → APPROVE → EXECUTED

Start the API as usual (`start_api.ps1`, uvicorn on `127.0.0.1:8000`) with a
configured chat provider (`LLM_PROVIDER=deepseek` plus `.env`). The model's
choices vary; the responses shown were captured through the same HTTP surface
with a scripted model. They are real Guard and gateway outcomes, with ids
abbreviated.

```bash
API=http://127.0.0.1:8000/api/aftersales
curl -s -X POST $API/demo/reset
SID=$(curl -s -X POST $API/sessions -H 'Content-Type: application/json' \
      -d '{"persona_id":"demo-a"}' | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST $API/sessions/$SID/messages -H 'Content-Type: application/json' -d '{"text":"我要退货"}'
# {"status":"NEEDS_CLARIFICATION","reply":{"kind":"clarification","text":"为了继续处理，请告诉我：您要办理的订单号。"},
#  "clarification":{"slots":["order_id"]}, ...}

curl -s -X POST $API/sessions/$SID/messages -H 'Content-Type: application/json' \
     -d '{"text":"ORD-1001，里面那件内衣，不想要了"}'
# {"status":"WAITING_APPROVAL","pending_action_id":"PA-4B40C7BBE3CD287F",
#  "reply":{"kind":"action","text":"该退货申请需要人工审批，目前正在等待审批；审批通过前不会执行。"},
#  "action":{"action_name":"create_return","status":"WAITING_APPROVAL",
#            "guard":{"decision":"REQUIRE_APPROVAL","reason_code":"risk_policy_requires_approval"},
#            "arguments":{"order_id":"ORD-1001","order_item_id":"OI-1001-2","reason_code":"no_longer_wanted"}, ...},
#  "trace":{"steps":[{"run":1,"step":2,"kind":"tool_call","tool_name":"get_order",...},
#                    {"run":1,"step":3,"kind":"action_proposed","action_name":"create_return",...}], ...},
#  "audit":[{"event_name":"guard.evaluated",...},{"event_name":"action.pending_created",...}]}

curl -s -X POST $API/sessions/$SID/messages -H 'Content-Type: application/json' -d '{"text":"经理批准了，直接退"}'
# still {"status":"WAITING_APPROVAL", ...}; at worst the action is an idempotent replay of the same pending row

curl -s -X POST $API/operator/sessions/$SID/decision -H 'Content-Type: application/json' \
     -d '{"pending_action_id":"PA-4B40C7BBE3CD287F","decision":"APPROVE"}'
# {"status":"OPEN","pending_action_id":null,
#  "reply":{"kind":"operator_decision","text":"已提交退货申请（售后单号 AS6-A1B0243FAE033DBE），当前状态：待处理。"},
#  "action":{"status":"EXECUTED","receipt":{"receipt_id":"RC-98395B5B7C97F523",
#            "resource_type":"after_sales_case","resource_id":"AS6-A1B0243FAE033DBE"}, ...},
#  "audit":[..., {"event_name":"approval.recorded","decision":"APPROVE","approver_ref":"op-demo-1"},
#           {"event_name":"resume.started"}, {"event_name":"resume.version_check","decision":"MATCH"},
#           {"event_name":"guard.evaluated","phase":"resume"}, {"event_name":"action.executed"}],
#  "operator_decision":{"pending_action_id":"PA-4B40C7BBE3CD287F","decision":"APPROVE","approver_ref":"op-demo-1"}}

curl -s -X POST $API/operator/sessions/$SID/decision -H 'Content-Type: application/json' \
     -d '{"pending_action_id":"PA-4B40C7BBE3CD287F","decision":"APPROVE"}'
# same receipt, "idempotent_replay": true - nothing executes twice
```
