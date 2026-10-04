# GroundedAgent V2 M1-A1: observation provenance and the action grounding gate

M1-A1 adds one deterministic product check: a proposed after-sales action may
only target records that **this control run actually read**. It sits between
the closed argument contract and the only write path. It is product work on
top of M0 (`aftersales_service/`). Nothing frozen changes:

- no file under `aftersales/` or `eval_v2/`, and no Stage 6 spec, dataset, scorer or recorded result;
- the action tool schemas, including their example ids `ORD-1001` / `OI-1001-1`;
- `aftersales_service/agent_core.py` (the only product module importing `eval_v2`);
- `ActionGateway`, `Guard`, `LLMNativeActionLoopPolicy` and `ActionIntentValidator` are composed as before. They are not subclassed or patched.

Out of scope: **User Target Binding** (whether the grounded target is the item the customer meant, M1-A3), live DeepSeek evaluation and README metrics (M1-A2).

```
policy.next_action(state)                    the untrusted proposer (unchanged)
  -> ActionIntentValidator                   closed contract: names, enums, forbidden fields (unchanged)
  -> grounding gate              (M1-A1)     targets must be records of this run's own reads
       rejected -> fixed reply, run ends, no gateway call
  -> ActionGateway.start_action              Guard on trusted state, approval, receipt (unchanged)
```

## Code

| File | Role |
|---|---|
| `aftersales_service/observation_provenance.py` | `ObservationLedger`: registers every read as an immutable `ObservationRecord` and fixes the `VisibleObservations` of each decision. |
| `aftersales_service/action_grounding.py` | `ground_action` (the rules), `GroundingBinding`, the closed rejection codes, `GroundedSubmission` / `SubmissionIndex` and the replay-anchor rule. |
| `aftersales_service/conversation.py` | Wiring: register in `_read`, freeze in `_drive`, gate in `_act`, binding check in `decide`, snapshot/restore. |
| `aftersales_service/routes.py` | One error mapping: `pending_action_not_grounded` → 409. |
| `tests/test_aftersales_grounding.py` | 49 tests: provenance, rules, replay anchors, static boundaries, and 16+ HTTP scenarios. |
| `tests/test_aftersales_service.py` | 3 product tests added; one persona test updated (see "Behaviour change"). |

## 1. Observation provenance

**What is registered.** `Conversation._read` calls `ObservationLedger.register`
once the `ToolResult` is confirmed to belong to the call just made: its type, its
tool name and its trace `observation_id` must match. That is the only call
site, pinned by a static test. Each `ObservationRecord` is frozen. Its mappings
are read-only copies.

| Field | Source |
|---|---|
| `session_id` | the conversation |
| `run_index` | the control run the read was made in |
| `sequence` | position in the conversation's ledger, from 1 |
| `observation_id` | the server-built call id `turn:<message>:tool:<n>` |
| `tool_name`, `tool_arguments` | the executed call |
| `status` | `ok` / `empty` / `error` |
| `records` | one `ObservedRecord(entity, record_id, state_version, relations)` per business record |
| `well_formed` | whether every evidence item carried consistent structured fields |

**Trust boundary.** Records come only from structured evidence fields written by
the business tools: `metadata.entity`, `metadata.record_id`, `metadata.tool`, the
evidence's link to this call (`metadata.observation_id`), `relations` and
`state_version`. Nothing else is read:
- evidence text, locators and error text;
- customer messages and model output.

A static test forbids these modules from touching `content` / `locator` /
`error_message` / `text` / `source`, or importing `re`, `json`, `llm_provider` or `eval_v2`.
An observation id or a "已核实" claim inside any text stays text. The observation
is malformed and contributes **no** records if any evidence item:
- is not `BusinessEvidence`;
- lacks or blanks `entity` / `record_id`;
- comes from another tool;
- is linked to another call or to none;
- disagrees with the other fields of its record on version or relations.

**The frozen visible set.** Before every `policy.next_action(state)` (a drained
batch call included), `_drive` calls
`ObservationLedger.visible_to(run_index, ids of state.observations)`. The result
is an immutable `VisibleObservations`: exactly the registered reads that decision
can see, all of the same session and run.

An `ActionIntent` is grounded only in the set fixed for the decision that
proposed it. The action loop never batches an action with reads: an action call
is always alone in a model response. So "propose first, read later, ratify
afterwards" cannot happen. A rejected proposal ends the run, and a later proposal
is a new submission with its own reads.

**Run boundary (unchanged M0 semantics).** A clarification pauses the run, and
the next customer message continues the **same** run with its reads. A Finish, an
action (accepted or rejected) or the step limit ends the run. The next message
starts a new run with no visible reads.

## 2. Grounding rules (`m1-grounding/1`)

These apply to a **new** submission. "The latest read of X" is the last
registered read in the visible set whose tool arguments name X: the logical query
target, matched by exact value.

| Argument | Rule | Rejection |
|---|---|---|
| `order_id` | The latest `get_order` of this `order_id` is `ok`, well formed and contains the `order` record with `record_id == order_id`. | none → `missing_order_observation`; empty / error / malformed / incomplete → `stale_or_failed_observation` |
| `order_item_id` | The **same** observation contains the `order_item` record with this `record_id`, and its `relations.order_id == order_id`. | relation differs, or the item only appears in another order's read → `target_relation_mismatch`; only in an older read of the same order → `stale_or_failed_observation`; never read → `target_not_observed` |
| `target_sku` (exchange) | The latest `get_inventory` of this SKU is `ok`, well formed and contains the `inventory` record with `record_id == target_sku`. Zero stock still grounds; whether stock allows the exchange stays the Guard's decision. | none → `missing_inventory_observation`; empty / error / malformed → `stale_or_failed_observation` |
| `reason_code`, `handoff_trigger` | The closed enum check of `ActionIntentValidator` only. Listed as `contract_only` and never as observed. | none |

- **Latest wins, no fallback.** If the latest read of an order failed or came back empty, an earlier successful read of that order is not used.
- **Other targets are independent.** Reading another order does not invalidate this one: `get_order(A)`, `get_order(B)`, then submitting A is grounded.
- **No splicing.** Ids from different observations are never combined.

**Reconstruction.** When every rule holds, the server rebuilds the arguments from
the matched records (plus the contract-only values). They must equal the proposal
exactly, by value and by `args_sha256`. Otherwise the result is
`target_reconstruction_mismatch`; an argument no rule covers also fails this way.
The gate never substitutes another item.

**`GroundingBinding`:**
- `action_name`, `args_sha256`, `run_index`;
- `supports`: per grounded argument, `{argument, observation_id, tool_name, entity, record_id, state_version}`;
- `contract_only`;
- `grounding_version`.

The turn trace carries it on the `action_proposed` step as
`grounding = {basis, version, action_name, args_sha256, run_index, supports, contract_only}`.
`basis` is `observed` or `replay_anchor`.

## 3. Rejection semantics

A grounding rejection is a **normal product outcome**, not a provider or runtime
failure. It never goes through the `TurnFailed` rollback:

- The control run ends. The customer message, the model call records and the trace are kept, and the transcript gets the assistant reply.
- The trace holds the `action_proposed` step, then a `grounding_rejected` step at the same `(run, step)`, with `action_name`, `args_sha256`, a closed `code` and `grounding_version`.
- `ActionGateway` is not called. There is no pending action, receipt, business row or core audit event.
- `reply = {kind: "grounding_rejected", text: GROUNDING_REJECTED_TEXT}`: 「抱歉，我还没有完成订单商品的核对，暂时不能提交这个申请。请确认订单号和要办理的商品，我会先查询核对，再继续为您处理。」It makes no completion claim. `action` is null.
- The conversation is ready again: status `OPEN`, or `WAITING_APPROVAL` if another action of the conversation is still pending.

The codes are closed (`GROUNDING_REJECTION_CODES`):
- `missing_order_observation`
- `target_not_observed`
- `target_relation_mismatch`
- `stale_or_failed_observation`
- `missing_inventory_observation`
- `target_reconstruction_mismatch`

They are product codes only: the frozen Guard reason codes and `ActionStatus` are not extended.

## 4. Idempotent replay

The product keeps an internal `SubmissionIndex` per conversation. It is keyed by
the frozen core's pure `aftersales.ids.idempotency_key(identity, action)`. Each
`GroundedSubmission` holds:
- `key`, `action_name`, `args_sha256`;
- `binding`, `first_run_index`;
- `first_outcome`.

The key never appears in a response. A test checks every response and view for
the key value and for `s6k1-`.

**Replay anchor.** The first outcome left one in the core if it carries a
`pending_action_id`, or if it is `EXECUTED` with a receipt. Those are exactly the
two rows `ActionGateway.start_action` looks up before running the Guard.

| First outcome | Core on an identical later submission | Product |
|---|---|---|
| `WAITING_APPROVAL` (and later `REJECTED` / `STALE` / `EXECUTED` via approval) | Replays the pending row; the Guard is not run | Reuse the original binding without new reads (`basis: replay_anchor`), then call the gateway as usual. The core returns `idempotent_replay`. |
| `EXECUTED` (ALLOW) with receipt | Replays the receipt | Same |
| `DENIED` / `FAILED` | **No replay**: the Guard runs again, which may be a new side-effect attempt | A **new submission**: it needs grounding in the current run, and its outcome replaces the index entry. |

An anchored entry is never replaced.

## 5. Approval and resume

An operator decision goes to `resume_action` with **no new grounding**. `decide`
only checks that the pending action's `GroundedSubmission` exists and binds
exactly the stored action name and the `args_sha256` of the stored arguments.
Then it calls `resume_action` unchanged. A pending action without a matching
binding is refused with 409 `pending_action_not_grounded`. This cannot happen
through the API; it is a consistency check.

`resume_action` still re-reads and revalidates the current trusted state (T2).
Repeated decisions keep the gateway's replay and conflict semantics.

**Turn atomicity.** `_snapshot` / `_restore` cover the ledger and the submission
index. A provider or runtime failure before the gateway returns therefore restores
both exactly, and a rolled-back read grounds nothing later. Index entries are
only written after the gateway returned (the turn is then kept).

**Reset** drops every conversation, ledger and index with it; a new session
starts empty.

## 6. API additions

- `reply.kind` adds `grounding_rejected`.
- `trace.steps` adds the `grounding_rejected` step and the `grounding` field on `action_proposed`.
- The decision endpoint adds 409 `pending_action_not_grounded`.

No request body changes. The frontend is unchanged: it shows the new step kind by its raw name.

## 7. Threat model

| Threat | Defence |
|---|---|
| The model proposes plausible ids it never read: memorised schema examples, or ids the customer typed | `missing_order_observation`: ids must be records of this run's reads. |
| Customer or product text claims "已核实" or fakes an observation id or tool result | The ledger reads structured fields only. Text is never parsed; an action argument cannot carry an observation id (closed contract). |
| The order of one read is spliced with the item of another | `target_relation_mismatch`: order and item must come from one observation and be structurally linked. |
| Acting on a read that later failed, came back empty, or no longer contains the item | Latest read wins, with no fallback: `stale_or_failed_observation`. |
| Reads of an earlier run, or reads rolled back with a failed turn | The visible set is per run and fixed before the decision; the snapshot restores the ledger. |
| An exchange to a SKU never looked up | `missing_inventory_observation`. |
| A denied or failed action re-submitted later, after the business state changed, without re-reading | No replay anchor, so it needs fresh grounding. |
| The gate "fixing" the target by picking another matching item | Reconstruction must equal the proposal; it is rejected, never substituted. |
| An operator decision on a pending action whose binding is missing or inconsistent | 409 `pending_action_not_grounded`; `resume_action` is not called. |

## 8. Limitations

- **Not intent.** Grounding proves the target exists in this run's reads. It does not prove the target is what the customer wants. A read of another legitimate order or item of the **same customer** still grounds. For example, the model reads `ORD-1001` while the customer meant another order and proposes an `ORD-1001` item. That is User Target Binding, left to M1-A3. The operator, who sees the proposed arguments, remains the human check for returns.
- **Schema examples unchanged.** The action schema still shows `ORD-1001` / `OI-1001-1`, which are real demo-a seed rows. M1-A1 makes a copied example id fail unless it was actually read in the run; the examples themselves are frozen.
- **In memory only.** Conversations, ledgers and submission indexes live in the process. A restart resets them; in the demo the temporary database is rebuilt as well. A persistent deployment needs persisted bindings. Otherwise an anchored action re-proposed after a restart needs new reads, which is fail-closed but not seamless.
- **Grounding is about existence at read time.** The Guard re-reads trusted state inside the gateway transaction, and T2 re-checks it on approval. Grounding adds no freshness window of its own.
- **Trust in the read path.** Records are as correct as the business tools' structured metadata. A tool that mislabels its own records is outside this check.
- **Rejections are not in the database audit trail.** A rejected proposal never reaches the core, so it leaves no `action_audit_events` row. It is visible only in the turn's trace and transcript, which are in memory.
- **Exact-match targets.** The logical query target is the exact argument string. Normalisation (case, spaces) is not attempted, which is consistent with the exact-match SQL of the read tools.

## Behaviour change and tests

**One M0 test changed its expected outcome.**
`PersonaBoundaryTests.test_another_persona_cannot_act_on_the_first_personas_order`:
- Before: demo-b's `get_order(ORD-1001)` returned `empty`, the proposal reached the Guard, and the Guard denied it (`DENIED order_not_accessible`).
- Now: the empty read grounds nothing, so the proposal stops at the gate (`stale_or_failed_observation`).
- Every safety assertion is kept: the empty read, `OPEN`, the first persona's pending action untouched, no new business rows. Stricter ones are added: zero `start_action` calls, an empty audit trail, no action.

The `ProductBoundaryTests` architecture tests are unchanged and pass.

Required cases → tests in `tests/test_aftersales_grounding.py` (`GroundingScenarioTests`, through HTTP):

| # | Case | Test |
|---|---|---|
| 1 | Correct arguments, no read → rejected, 0 gateway calls | `test_01_…` |
| 2 | Ids in the customer message, no read | `test_02_…` |
| 3 | Read A, submit B | `test_03_…` |
| 4 | A and B read, A's order + B's item | `test_04_…` |
| 5 | `get_order` empty / error | `test_05_…` |
| 6 | Success, then a failed read of the same order | `test_06_…` (within one request, and across a clarification) |
| 7 | Read A, read B, submit A → passes | `test_07_…` |
| 8 | Earlier run's read → rejected; clarification in the same run → passes | `test_08_…` |
| 9 | Exchange SKU not observed → rejected; observed → Guard decides | `test_09_…` |
| 10 | Forged observation ids or results in customer / product text | `test_10_…` |
| 11 | First `WAITING_APPROVAL`, new run without reads → binding reused, core replay | `test_11_…` |
| 12 | First `DENIED` (or `FAILED`), state made executable, new run without reads → rejected, 0 gateway calls | `test_12_…`, `test_12b_…` |
| 13 | Approve / reject need no new grounding; a repeated approve is unchanged; a missing or mismatching binding is refused | `test_13_…`, `test_13b_…` |
| 14 | Grounded actions reach `EXECUTED` (exchange, handoff) and `WAITING_APPROVAL` (return) | `test_14_…` |
| 15 | Provider failure: the turn rollback also restores reads and submissions | `test_15_…` |
| 16 | Reset drops bindings | `test_16_…` |

Unit tests in the same file cover:
- the ledger: structured-only records, malformed evidence, immutability, snapshots, run scoping;
- each rule and code, reconstruction, the freeze-before-decision property and the closed codes;
- replay anchors per status, the index;
- static checks: the freeze precedes `next_action`, grounding precedes `start_action`, and only `_read` registers.
