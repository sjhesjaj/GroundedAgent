# GroundedAgent V2 M3: after-sales knowledge base as an agent tool

Revision 4 (two code reviews against main `ec21b40`).

## The problem

The project was meant to be two things in one product: a knowledge base that
answers after-sales policy questions (the V1 retrieval stack, retargeted to
after-sales rules) and an agent that handles returns, exchanges and hand-offs.
V2 built the second half deeply and the first half thinly: the after-sales
"knowledge" is 6 structured rules (`policy_sources/`), searched by token
matching plus date / category / priority selection; V1's retrieval (chunking,
BM25 + embedding + RRF, rerank, refusal) is not used in V2.

M3 adds a real after-sales knowledge base that the agent searches as a tool and
answers from with citations.

## Reference designs

| Project | What M3 takes from it |
|---|---|
| LangGraph customer-support tutorial (Swiss Airlines bot) | Policy retrieval is a tool the model calls; sensitive actions keep their own gates. |
| OpenAI `openai-cs-agents-demo` | FAQ role and relevance guardrail; here the existing `boundary` / `refuse` dispositions play that role. |
| Sierra τ-bench / τ²-bench | Returns / exchanges under a policy, scored on final database state; **pass^k**; τ²'s `banking_knowledge` domain as a reference for corpus and task design. |

## Principle: retrieval explains, structured rules decide

What M3 guarantees about KB text, stated exactly:

1. **It cannot make the Guard allow an action.** The Guard reads only the
   structured catalog and trusted state.
2. **It cannot ground an order or item id.** The provenance ledger registers
   only `BusinessEvidence` (`observation_provenance.py`); KB passages are never
   business records. A regression test pins this.
3. **It cannot say anything about eligibility that the rules do not say.** See
   "Eligibility content" below.

What M3 does *not* claim: KB text can influence *which* action the model
proposes (an FAQ saying "质量问题可联系人工" makes a hand-off more likely). That
is intended behaviour, bounded by 1-3.

### Eligibility content

`policy_sources/*.md` is pinned by frozen tests, so no structured rule can be
added. Therefore:

- Every statement that affects eligibility (window lengths, non-returnable
  categories, hand-off conditions, promotion overrides of those) appears in the
  KB **only as a restatement of one of the 6 rules**, with `restates` mandatory.
- A lint fails the corpus build when a document without `restates` contains an
  eligibility pattern (e.g. "N 天" next to 退/换, a category exclusion phrase).
- Restated numbers must equal the rule's params (build-time check).
- KB-only promotions and exceptions cover non-eligibility matters only:
  shipping cost, gifts, refund timing, required evidence, price protection.
- Precedence between restated rules is not re-implemented in the KB: it comes
  from `catalog.select(as_of=...)`, which already keeps an active promotion
  from being displaced by the standard rule. The demo business time
  (2026-11-15, `aftersales/demo.py`) has the November promotion in force: the
  first test case.

## Constraints found in the code

1. The model-visible tool list is fixed in the frozen evaluated loop
   (`eval_v2/action_loop.py`, `STAGE5_RUNTIME_TOOLS`); tool descriptions live in
   frozen `aftersales/registry.py`; Stage 5 `translate` rejects unknown tool
   names (`tool_loop.py`).
2. `CapabilityGate` and the product `ReadSide.execute` (`demo_store.py`) are bound
   to the frozen 5 read tools.
3. The frozen Stage 6 runner allows only those 5 tools
   (`action_runner.py`) and has no read-gateway injection point.
4. Only `agent_core.py` may import `eval_v2`, only the reused modules; no
   subclassing of the evaluated policy; `formal=` may only be the constant
   `False`; the product package may not contain `holdout`, `expected_`, or read
   the system clock.
5. The frozen evidence derivation accepts evidence from any tool and validates
   `policy_ref` only for `search_after_sales_policy` (verified offline: a KB
   document evidence passes derive -> `build_sources` -> `build_messages`).
   `generation.py` attaches any evidence carrying a `policy_ref` key to window
   facts, so KB evidence must not use that key.

## Design

### Decision policy `m3-decision/1`, by composition

Not a copy (`agent_core.py` explicitly rejects a ~1,500-line drifting copy).
`aftersales_service/decision_policy.py` composes the reused `tool_loop` /
`action_loop` helpers through `agent_core` re-exports and replaces exactly four
things:

1. the system prompt: rule 1 lists the tools and states the split - windows,
   non-returnable categories and hand-off conditions -> `search_after_sales_policy`;
   shipping, refund timing, evidence, promotions' non-eligibility terms, FAQ ->
   `search_knowledge_base`;
2. the offered functions;
3. the tool schemas;
4. translation, so the new tool name is accepted;
5. **previous replies as context:** the decision and answer prompts include
   the conversation's earlier final replies as assistant messages. Today a run
   sees every customer message but neither its own earlier replies nor earlier
   runs' observations (`tool_loop.build_messages` has no assistant text), so
   "你刚才说的 15 天从哪天算？" or "第二个呢？" cannot be understood. Earlier
   replies are context only: they are not observations, cannot ground an id
   (the gate still requires this run's reads), and are capped in number and
   length.

No `formal` mode. The fail-closed parsing and one-call-per-decision behaviour
are reused unchanged.

### Selection, default and sessions

- `AFTERSALES_DECISION_POLICY=stage6|m3`, **default `stage6`** until Phase 6
  passes; switched to `m3` in the same PR that records the results.
- A session is bound to the policy version it was created with (stored in the
  session file). Loading it under another version is refused
  (`policy_version_mismatch`, 409) rather than replaying a tool call the other
  policy does not know.

### Tool `search_knowledge_base`

- Input: `query` only. Wired in the product: a product-side read executor next
  to `ReadSide`, not through `CapabilityGate` / the frozen registry.
- Retrieval: BM25 + embedding with **`bge-m3`** via Ollama (`nomic-embed-text`
  measured far worse on Chinese in this project's V1 history), RRF; filters:
  effective at business time, category scope when named; restated-rule
  passages carry `restates_policy_id` (never `policy_ref`).
- Output caps: at most 4 passages, each at most ~400 characters (M2 measured
  observations at 94 % of checkpoint state).
- Every result records its retrieval mode. If Ollama or `bge-m3` is missing,
  the product falls back to BM25 and says so; an **evaluation run with any
  fallback is void**, and the runner checks both before starting.

### Answer layer

Reuse the evaluated generation layer unchanged (constraint 5). Fork an
`m3-answer/1` only if Phase 6 shows it is needed.

### Customer-facing wording under `m3`

The non-answer dispositions render frozen fixed texts ("根据现有证据无法可靠回答。",
"该问题需要人工进一步处理。", "当前只读能力无法执行该操作。"). The last one is a
Stage 5 leftover and is now false (the product does act); with more
unanswerable KB questions these texts will appear more often, including after
"谢谢". Under `m3` only, `conversation._finish` renders product wording instead
(polite refusal with what the assistant *can* do, a hand-off offer, a
greeting / thanks reply). The frozen texts stay for `stage6`, so its golden is
unaffected.

### Out of the product's scope (stated in the README)

No refunds, payments or address changes; no pre-sales product questions
(sizes, specs); one action per message ("退 A 换 B" takes two messages).

## Corpus

- `knowledge_base/`, one Markdown file per document; front matter `doc_id`,
  `title`, `doc_type`, `scope`, `effective_from`, `effective_to`, `version`,
  `restates` (mandatory for eligibility content).
- **40-60 documents**, dense with traps (near-duplicates, superseded versions,
  promotion vs standard wording, category notes), rather than 100 thin ones;
  the user reviews all of them. Design informed by τ²-bench `banking_knowledge`.
- Frozen before the eval sets are written.

## Evaluation (`eval_m3/`)

Datasets frozen before tuning; holdout written in an isolated context, opened
once. Lexical overlap between questions and gold passages is capped and
checked with `check_evaluation_overlap.py` (model-written corpus and questions
otherwise inflate BM25).

| Set | Size | Content |
|---|---|---|
| KB-DEV | ~50 | Policy questions (single / multi-doc, superseded versions, promotion in force, category notes, unanswerable), action and mixed cases ("运费谁出？那帮我退了"), **follow-ups that refer to the previous reply**, and small talk / thanks / off-topic with a labelled expected disposition |
| KB-HOLDOUT | ~25 | Same mix, sealed |
| Stage 6 DEV subset | 24 cases, frozen list: s6-dev-001, 004-017, 019-025, 039, 040 (018 has timeout injection; 002, 003, 026-038 have operator scripts) | Safety with the KB tool present; each case seeds the product database from its own `initial_state` (ORD-30xx), not the demo data |

Runners:

- **Drift check:** the frozen Stage 6 runner with `m3` on full Stage 6 DEV. The
  KB tool is absent there (constraint 3), so this proves only that the
  composed policy did not change Stage 6 behaviour.
- **`eval_m3` runner:** drives the product `Conversation` (scripted personas,
  fixed clock, per-case temporary data dir) for KB-DEV, KB-HOLDOUT and the
  Stage 6 DEV subset, scoring final database state and the 6 hard invariants
  with the KB tool available.
- **Scripted equivalence:** the 45 golden scenarios run under `m3` must return
  HTTP payloads identical to the `stage6` fixture **excluding
  `trace.model_calls`** (which carries `offered_functions`, now including the
  KB tool). Model requests differ by design; outcomes must not. Scenarios that
  end in a non-answer disposition are compared after mapping the `m3` wording
  back, or listed as expected differences.

Metrics, defined mechanically:

- Retrieval: Recall@1/3/5. Single-gold: hit if the gold `doc_id` and version is
  in the top k. Multi-gold: report "all gold in top k" and "any gold in top k"
  separately. A superseded version counts as a miss. Ablation: BM25, vector,
  hybrid, hybrid + rerank; reported as is, including "no difference".
- Routing: each case labels the expected tool route; score routing accuracy
  (not "did it call the KB").
- Answers: `must_include` and `must_not_include` key facts (during the
  November promotion "7 天" is wrong); citation hit = cites at least one gold
  document (validity is already guaranteed by `parse_answer`).
- Rule consistency, hard: every day count and category verdict in an answer is
  compared with the params of the rule `catalog.select(as_of=virtual_now)`
  returns for that category; one mismatch fails the case.
- Reliability: pass^3 on KB-DEV and the Stage 6 DEV subset.
- Step-limit rate (6 steps per run; a mixed case uses up to 5).
- Latency: end-to-end per turn, p50 / p95 (2-4 decision calls, 1 generation
  call, retrieval; no streaming). Measured in Phase 0 and in Phase 6.
- Follow-up resolution: share of follow-up cases answered correctly.
- Disposition accuracy on small talk / thanks / off-topic cases.
- Safety: 6 hard invariants hold in every `eval_m3` run; any violation blocks
  the milestone.

## Phases

| Phase | Deliverable and acceptance | CC time |
|---|---|---:|
| 0. Spike | `m3-decision/1` by composition with the 5 replacements; product read executor; 3 hand-written docs; real DeepSeek on ~5 policy, mixed and follow-up questions: routing, citations, step use, **per-turn p50 / p95 latency**; `stage6` golden byte-identical; scripted equivalence under `m3` (excluding `trace.model_calls`); Ollama + `bge-m3` latency on Windows. | 1-1.5 h |
| 1. Corpus | 40-60 docs; loader, heading-aware chunker, index with embedding cache (model + content hash; offline tests use the cache); eligibility lint; restated-number check; user review. | 1.5-2.5 h + review |
| 2. Eval sets | KB-DEV and KB-HOLDOUT (isolated, sealed); overlap check. Before any tuning. | 1.5-2 h |
| 3. Retrieval | Filters, fallback recording, ablation on KB-DEV, rerank decision. | 1.5-2 h |
| 4. Runtime | Session policy binding and refusal; passage caps; KB-never-grounds test; earlier replies as context (capped) with a test that they never ground an id; `m3` customer-facing wording; frontend KB citations. | 2-3 h |
| 5. `eval_m3` runner | Conversation-driven runner, invariants, metrics above, environment preflight. | 2.5-3.5 h |
| 6. Results | Drift check; KB-DEV and Stage 6 subset with pass^3; KB-HOLDOUT once; switch default to `m3`; results, README, resume line. | 2-3 h |

Total about 15-21 h of CC time, about 4-5 calendar days including your corpus
review.

## Out of scope

Multi-agent routing; passages in Guard or grounding decisions; changes to
frozen Stage 6 code, rules or datasets; reopening the Stage 6 holdout; a vector
database.

## Risks

- **Corpus quality and review time.** 40-60 dense documents is still a real
  review; it is the gate for everything after it.
- **Routing errors.** Two policy tools with overlapping topics; measured by
  routing accuracy and pass^3.
- **Step budget.** Mixed questions can hit the 6-step limit; measured.
- **Latency.** Several model calls per turn without streaming; if p95 is
  poor, streaming the final answer is a follow-up, not part of M3.
- **Ollama on the demo machine.** BM25 fallback in the product; evaluation runs
  refuse to start without `bge-m3`.
