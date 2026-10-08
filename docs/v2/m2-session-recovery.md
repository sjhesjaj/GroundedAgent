# GroundedAgent V2 M2: LangGraph control flow and crash-safe session recovery

## The problem

The after-sales service keeps every conversation in process memory
(`AftersalesService._sessions`) and the demo business database in a temporary
directory (`DemoStore`). A restart therefore loses:

- every conversation, including one paused on a clarification question;
- the pending-action ↔ grounding bindings a later operator decision needs
  (`Conversation._actions`, `SubmissionIndex`), so a pending return can no longer be approved;
- the business database itself.

There is also a crash window the in-process rollback (`_snapshot` / `_restore`) cannot cover:
the `ActionGateway` has committed a return, the process dies, and the
conversation never learns the outcome. Re-asking the model what it did is not
acceptable - it may propose something else.

M2 makes the product control flow a LangGraph `StateGraph` with a SQLite
checkpointer so that a conversation survives a restart, and closes the crash
window by re-running **only** the gateway node, relying on the core's existing
idempotency.

## Scope

In scope: `aftersales_service/` (product layer), its tests, a fixed demo data
directory, README/docs.

Unchanged, zero diff: `aftersales/`, `eval_v2/`, `eval/`, every frozen spec,
dataset, scorer and recorded result. `agent_core.py` stays the only product
module importing `eval_v2`. The sealed holdout is **not** reopened; its results
stay attributed to `v2-stage6-final`.

Out of scope: multiple processes or instances, PostgreSQL, real operator
authentication, M1-A3 target binding.

## Phase 0 findings (spike, done)

A throw-away spike (not committed) ran a graph of the same shape against the
real `ActionGateway` and demo seed, each step in its own process, crashes
simulated with `os._exit` (no checkpoint write, no `finally`).

| Check | Result |
|---|---|
| Crash right after `start_action` returned, default `durability="async"` | **Lost.** The checkpoint holding the validated action had not reached disk when the gateway node ran; recovery saw no submission while `pending_actions` had 1 row. |
| Same crash, `durability="sync"` | Recovered. Re-running the gateway node returned the core's idempotent replay (`replay: true`), 1 pending row, step count and the clarification answer intact. |
| Clarification paused (`interrupt`), process exits, new process sends `Command(resume=...)` | The run continued with its step budget. |
| Operator APPROVE, crash right after `resume_action` | `get_outcome` reported `EXECUTED`; a repeated APPROVE was an idempotent replay; exactly 1 receipt and 1 new case. The conversation's stored status was stale until reconciled from the gateway. |
| Model failure after a read, mid-turn | The dirty checkpoint kept the failed turn's message; the next turn forked from the committed head and did not see it. |
| Project dataclasses through LangGraph's default serializer | Round-trip equal, but LangGraph warns that deserializing unregistered types "will be blocked in a future version". |

Decisions taken from the spike:

1. Every invocation uses `durability="sync"`. This is the precondition of the whole recovery design.
2. The committed version of a conversation is a **head pointer** (checkpoint id) outside the graph. Turns run from the head; a failed or crashed turn leaves an orphan branch that is never read.
3. Graph state holds only JSON-native values produced by an explicit, versioned codec; no project class goes through the checkpointer's msgpack extension path.
4. Recovery never calls the model. The only node it may re-run is the gateway node.
5. Operator decisions are reconciled from `ActionGateway.get_outcome`, never re-decided.

## Design

### Graph

```
START -> begin_run ----------------------------------------> decide
         (a new independent run: step 1, own observations)

decide --ToolCall-----> read --------------------------------> decide
       --Clarify------> clarify  [interrupt]  -- resume ----> decide
       --ActionIntent-> ground --rejected---------------------> END (fixed reply)
                              --grounded: submission saved--> gateway -> END
       --Finish-------> finish (answer layer / fixed text) ---> END
       --step limit---> step_limit ---------------------------> END
```

- `decide` builds `ActionControlState` exactly as `_drive` does today and calls the evaluated policy once.
- `clarify` contains nothing before `interrupt()`; on resume it only appends the customer message (LangGraph re-runs an interrupted node from its first line).
- `ground` = today's contract check + grounding gate + replay-anchor lookup. It writes the **submission** (action name, canonical args, idempotency key, binding, basis) into state. With `durability="sync"` this is on disk before `gateway` starts.
- `gateway` is `Conversation._act` reduced to: rebuild the `ValidatedAction` from the persisted submission, `start_action` once, record the outcome. It stays a function named `_act` in `conversation.py` (boundary test `test_an_approval_is_built_in_exactly_one_place`).
- Runtime objects (policy, guarded provider, read side, store) reach nodes through LangGraph's runtime context, never through state.

### State and codec

`conversation_state.py` (new): `ConversationState` with a `schema` version and
the eleven pieces `_snapshot()` captures today - messages, observations,
transcript, run/tool-step/sequence counters, the open run, actions, open
pending ids, the observation ledger and the submission index - plus the
per-turn trace and the in-flight submission. Each project type gets
`encode`/`decode` to plain dicts; a decode that does not reproduce an equal
object is an error. Unknown `schema` versions are refused, not migrated.

### Persistence layout

```
<data dir>/                 AFTERSALES_DATA_DIR, default .aftersales-demo/ (git-ignored); tests use a temp dir
  generation.json           {"generation": "<uuid>"}       atomically replaced
  gen-<uuid>/
    aftersales-demo.db      the business database (seeded once per generation)
    checkpoints.db          LangGraph SqliteSaver
    sessions/<id>.json      {schema, persona_id, generation, head}  atomically replaced
```

The head pointer lives in a JSON file because the product package may not
contain SQL (`test_the_product_never_writes_sql`). Reset creates a new
generation and switches `generation.json`; a session file from another
generation is `session_not_found`, so an old conversation can never resume
against a new database. Ids stay collision-free across restarts:
`DeterministicIdProvider` derives them from the idempotency key.

### Turn semantics

| Where it stops | What the customer sees | What happens on the next load |
|---|---|---|
| Model / tool failure before `ground` saved a submission | `TurnFailed`, as today | Head unchanged; the orphan branch is ignored. |
| Process dies before a submission is saved | Connection error | Same as above. |
| Process dies after the submission is saved, gateway may or may not have committed | Connection error | Re-run `gateway` from that checkpoint: committed → idempotent replay of the real outcome; not committed → the grounded action is submitted now (the Guard re-reads trusted state). Head advances, the reply joins the transcript. |
| Paused on a clarification | The clarification | Resume with the next message (`Command(resume=...)`). |
| Process dies after `resume_action` | Connection error | Every tracked pending id is refreshed from `get_outcome`; a decision the transcript missed is appended once. |

## Phases

| Phase | Deliverable and acceptance | Estimate |
|---|---|---:|
| 0. Spike | This document's findings. | done |
| 1. Golden + codec | Before any product change, record a golden file on `main`: for every scripted scenario in `tests/test_aftersales_service.py` and `tests/test_aftersales_grounding.py`, the provider requests (`ScriptedProvider.requests`) and the HTTP payloads. Codec with round-trip tests for every state type. | 1 day |
| 2. Graph | `_drive` replaced by the graph with an in-memory checkpointer. All existing product and grounding tests pass unchanged; the golden file matches byte for byte. | 1–1.5 days |
| 3. Persistence | Fixed data directory, `SqliteSaver` with `durability="sync"`, session files and head pointer, load/recover, reset generations. | 1 day |
| 4. Crash tests | Subprocess tests ending in `os._exit` at each row of the turn-semantics table, plus reconciliation. | 0.5–1 day |
| 5. Wrap-up | Full offline suite, Windows demo restart walkthrough, README, this document's results section. | 0.5 day |

Remaining after phase 0: **4–5 days.** Stop at the acceptance list below; no
further fault-injection campaigns in this milestone.

## Acceptance

1. Paused on a clarification, the process restarts; the next message continues the same run with its step budget.
2. Waiting for approval, the process restarts; the operator can still approve, with the original grounding binding.
3. Killed after `start_action` committed and before the conversation saved: after recovery exactly one business write, and the conversation shows the real outcome.
4. Killed after `resume_action`: after recovery the status is reconciled, a repeated decision is a replay, one receipt.
5. A failed turn leaves no trace in the committed conversation; recovering twice appends nothing twice.
6. After reset, no old session or old interrupt resumes against the new database.
7. All existing safety tests (guessed ids, wrong target, stale observations, cross-persona) and product boundary tests pass; the golden equivalence file matches; `git diff main -- aftersales eval_v2 eval` is empty.

## Dependencies

`langgraph==1.2.14`, `langgraph-checkpoint-sqlite==3.1.1` (pinned in
`requirements.txt`).
