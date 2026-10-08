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
| Clarification paused (`interrupt`), process exits, new process sends `Command(resume=...)` | The run continued with its step budget. (Superseded: review later showed a failed resume leaves its answer on the head; clarifications no longer use `interrupt`.) |
| Operator APPROVE, crash right after `resume_action` | `get_outcome` reported `EXECUTED`; a repeated APPROVE was an idempotent replay; exactly 1 receipt and 1 new case. The conversation's stored status was stale until reconciled from the gateway. |
| Model failure after a read, mid-turn | The dirty checkpoint kept the failed turn's message; the next turn forked from the committed head and did not see it. |
| Project dataclasses through LangGraph's default serializer | Round-trip equal, but LangGraph warns that deserializing unregistered types "will be blocked in a future version". |

Decisions taken from the spike:

1. Every invocation uses `durability="sync"`. This is the precondition of the whole recovery design.
2. The committed version of a conversation is a **head pointer** (checkpoint id) outside the graph. Turns run from the head; a failed or crashed turn leaves an orphan branch that is never read. The spike found the branch to recover as "the newest checkpoint of the thread"; review showed that is not sound (a later fork would hide it), so the design uses an explicit in-flight marker instead (see "Commit protocol").
3. Graph state holds only JSON-native values produced by an explicit, versioned codec; no project class goes through the checkpointer's msgpack extension path.
4. Recovery never calls the model. The only node it may re-run is the gateway node.
5. Operator decisions are reconciled from `ActionGateway.get_outcome`, never re-decided.

## Design

### Graph

```
START -> begin_run ----------------------------------------> decide
         (a new independent run: step 1, own observations;
          or, if a run is open on a clarification, continue it)

decide --ToolCall-----> read --------------------------------> decide
       --Clarify------> clarify (store awaiting slots) -------> END
       --ActionIntent-> ground --rejected---------------------> END (fixed reply)
                              --grounded: submission saved--> [stop] gateway -> END
       --Finish-------> finish (answer layer / fixed text) ---> END
       --step limit---> step_limit ---------------------------> END
```

- `decide` builds `ActionControlState` exactly as `_drive` does today and calls the evaluated policy once.
- **Every customer message enters as ordinary input from the head**, including the answer to a clarification. `clarify` ends the graph and keeps the open run with its `awaiting_slots` in state, as `_ControlRun` does today; `begin_run` continues that run (same step budget) instead of starting a new one.
- **No `interrupt()` / `Command(resume=...)` for clarifications.** Review reproduced that resuming an interrupt does not fork a new checkpoint: the answer is stored on the head itself, so after a failed attempt (answer "ORD-1001", model outage) a retry from the same head with "ORD-3015" reaches `decide` with **ORD-1001** - a wrong-order action and a failed turn that left a trace. Ordinary input from the head forks cleanly. The only static stop in the graph is `interrupt_before=["gateway"]`, used by the commit protocol, and it is resumed with `None`, never with customer data.
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

### Commit protocol (revised after review)

`durability="sync"` only orders checkpoint writes before the next node. It
does not make the checkpoint database and the business database one
transaction, and it does not say which branch of a thread is committed. The
product decides that with two fields of the session file, always written
together by one atomic replace:

```json
{"schema": 1, "persona_id": "demo-a", "generation": "<uuid>",
 "head": "<checkpoint id>",
 "inflight": null | {"checkpoint_id": "<id>", "idempotency_key": "<key>",
                     "action_name": "create_return", "args_sha256": "<sha>"}}
```

- `head`: the committed version. Every turn runs from it; nothing else is ever
  read as the conversation.
- `inflight`: present exactly while a business write may exist that the head
  does not record. Recovery finds the submission **only** through this marker,
  never by guessing "the newest checkpoint of the thread".

The graph is compiled with `interrupt_before=["gateway"]`, so a grounded turn
stops on a checkpoint whose `next` is `("gateway",)` and the service can read
that checkpoint's id. One action turn, in order:

1. The graph runs `begin_run … ground` and stops before `gateway` (checkpoint G, on disk: sync).
2. Write the session file with `inflight = {G, key, action, args digest}`. Before this, no business write can exist: a crash here discards the branch.
3. Resume from G: `gateway` calls `start_action` once.
4. Write the session file with `head = <final checkpoint>` and `inflight = null`, in the same replace.
5. Only then answer HTTP.

A clarification follows the same rule: the checkpoint that ends on
`clarify` becomes the head (step 4) **before** the question is sent, so a
customer who has seen the question can always answer it. A failed turn writes
nothing; its branch is never read.

**Failures after the marker write** must not lock a session for good:

- `start_action` raised: the core rolled back, nothing was committed. Clear the marker (head unchanged) and answer `TurnFailed`, as today.
- Anything fails after `start_action` returned (including the final session-file write): drop the in-memory conversation; the next request reloads it and runs recovery.
- `os.replace` on Windows may raise `PermissionError` while another process holds the file: retry briefly (a few attempts, short backoff) before giving up.

**Operator decisions** are not graph nodes. `decide` runs `resume_action`,
then writes the result with `update_state(head, …, as_node="finish")` (a node
whose only successor is END, so the stored run state - including an open
clarification - is untouched) and moves the head in one session-file write.
Review showed that `as_node="decide"` would end an open run; a test covers a
session that has a pending action from run 1 while run 2 waits on a
clarification.

**Recovery** runs when a session is loaded, under the session lock, before any
customer message or operator decision touches it:

| Session file says | Recovery |
|---|---|
| `inflight` is null | Nothing to do; reconcile pending ids (below). |
| `inflight` set | Load checkpoint `inflight.checkpoint_id`; refuse unless its generation matches, its `next` is exactly `("gateway",)` and its submission has the marker's key, action and args digest. Resume it (the only node that can run is `gateway`): committed before → the core's idempotent replay; not committed → submitted now, the Guard re-reading trusted state. Then step 4. |
| Recovery itself fails | The session answers `recovery_pending` (409) and accepts nothing else; it never forks a new turn past an unresolved marker. |

**Startup scan.** Recovery also runs once at start-up for every session file
of the current generation whose `inflight` is set, so a crashed action is
recorded (and becomes approvable) even if that customer never comes back.

**Reconciliation** of operator decisions: every tracked pending id is
refreshed from `ActionGateway.get_outcome`. Operator outcomes are the only
transcript entries that can be appended outside a committed turn, so only they
carry an event id, `decision:<pending id>:<status>`; an event id already in the
transcript is never appended again.

**Approval recorded, not executed.** `resume_action` is two transactions:
T1 records the decision, T2 (APPROVE only) revalidates and executes. A crash
between them leaves the pending action `WAITING_APPROVAL` with
`approval_recorded = true`. Recovery cannot finish it: `execute_approved` may
only be reached through `decide` (`test_an_approval_is_built_in_exactly_one_place`).
The view shows `approval_recorded`, and the operator repeats APPROVE: T1
replays, and `resume_action` continues into T2.

**Concurrency.** Single process. The existing per-conversation lock
serializes turns, decisions and recovery of one session and now also covers
its session-file writes; the registry lock still serializes reset against
everything else, and reset switches the generation only while holding every
conversation lock. A test drives a customer turn and an operator decision on
one session concurrently.

### Reliability contract

M2 promises, for the tested single-process abnormal exits: a conversation can
be recovered, and every business effect happens at most once and is recorded
in the conversation by the next start-up of the service. An approval recorded
but not executed at the crash needs the operator to repeat APPROVE. The audit
trail may show a second Guard evaluation for a DENIED or FAILED action after
recovery (the core keeps no replay record for those); the business effect is
still at most one. That comes from **persisted
submission + recovery marker + the core's stable idempotency key**, not from
LangGraph alone. Not covered: power loss or OS crash (both SQLite databases
use WAL with default `synchronous`), disk corruption, several processes or
instances.

### Turn semantics

| Where it stops | What the customer sees | What happens on the next load |
|---|---|---|
| Model / tool failure, anywhere before step 2 | `TurnFailed`, as today | Head unchanged; the orphan branch is never read. |
| Process dies before step 2 | Connection error | Same as above. |
| `start_action` raises after step 2 | `TurnFailed` | Marker cleared; head unchanged. |
| Process dies between step 2 and step 4 | Connection error | Marker recovery, at start-up or on load. |
| Ended on a clarification | The clarification (only after its head was written) | The next message is ordinary input; `begin_run` continues the open run. |
| Process dies between T1 and T2 of an APPROVE | Connection error | `approval_recorded` shown; the operator repeats APPROVE. |
| Process dies after `resume_action` | Connection error | Reconciliation; the decision event is appended once. |

## Phases

| Phase | Deliverable and acceptance | Estimate |
|---|---|---:|
| 0. Spike | This document's findings. | done |
| 1. Golden + codec | Before any product change, record a golden file on `main`: for every scripted scenario in `tests/test_aftersales_service.py` and `tests/test_aftersales_grounding.py`, the provider requests (`ScriptedProvider.requests`) and the HTTP payloads. Session ids are pinned for the recording by patching `aftersales_service.service.uuid` in the recorder (not `uuid.uuid4` globally); they enter `request_id`, hence the idempotency key and every pending/receipt id, so M2 keeps session-id creation in the same place. Business time is already the `FixedClock`. Codec with round-trip tests for every state type. | 1 day |
| 2. Graph | `_drive` replaced by the graph with an in-memory checkpointer. All existing product and grounding tests pass unchanged; the golden file matches byte for byte (same model calls, tool order, step budget). | 1–1.5 days |
| 3. Persistence | Fixed data directory, `SqliteSaver` with `durability="sync"`, session files with head and in-flight marker, the commit protocol and its failure rules, load/recover and the start-up scan, operator decisions via `update_state(as_node="finish")`, decision event ids, reset generations, the concurrency test. | 1.5–2 days |
| 4. Crash tests | Subprocess tests ending in `os._exit`: before and after the marker write, inside `gateway` after `start_action`, before and after the head write, between a clarification checkpoint and its head write, between T1 and T2 of an APPROVE, after `resume_action`; each followed by two recoveries. Plus the in-process retry test: clarification answered, model fails, retry with a different answer must reach `decide` with the new answer. | 1 day |
| 5. Wrap-up | Full offline suite, Windows demo restart walkthrough, README, this document's results section. | 0.5 day |

Remaining after phase 0: **5–6 days.** Stop at the acceptance list below;
no further fault-injection campaigns in this milestone.

## Acceptance

1. Paused on a clarification, the process restarts; the next message continues the same run with its step budget.
2. Waiting for approval, the process restarts; the operator can still approve, with the original grounding binding.
3. Killed after `start_action` committed and before the conversation saved: after recovery exactly one business write (cases, pending actions, receipts - not audit rows), and the conversation shows the real outcome; the same holds when the customer never returns and only the start-up scan runs.
4. Killed after `resume_action`: after recovery the status is reconciled, a repeated decision is a replay, one receipt. Killed between T1 and T2: `approval_recorded` is shown, a repeated APPROVE executes once.
5. A failed turn leaves no trace in the committed conversation, including a failed answer to a clarification: a retry with a different answer reaches `decide` with the new answer. Recovering twice appends no event id twice.
6. After reset, no old session resumes against the new database.
7. A session with an unresolved marker accepts no new turn until recovery succeeds; a `start_action` exception after the marker write does not leave one.
8. A concurrent customer turn and operator decision on one session serialize; no session-file update is lost. An operator decision on a session waiting on a clarification leaves that clarification answerable.
9. All existing safety tests (guessed ids, wrong target, stale observations, cross-persona) and product boundary tests pass; the golden equivalence file matches; `git diff main -- aftersales eval_v2 eval` is empty.

## Dependencies

`langgraph==1.2.14`, `langgraph-checkpoint-sqlite==3.1.1` (pinned in
`requirements.txt`). The checkpointer is built as
`SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))`
(the `LANGGRAPH_STRICT_MSGPACK` environment variable is read once at import
time, so setting it at start-up is unreliable). Note that in 1.2.14 strict
mode does **not** raise: a blocked project object comes back as a plain
`dict` with only a log line (checked). So the guarantee comes from the codec
itself: `decode` validates its input's type and `schema` and raises on
anything else, and a test asserts that every value in a stored checkpoint is a
JSON-native type.
