# GroundedAgent V2 M3: after-sales knowledge base as an agent tool

## The problem

The project was meant to be two things in one product: a knowledge base that
answers after-sales policy questions (the V1 retrieval stack, retargeted from
enterprise rules to after-sales rules) and an agent that handles returns,
exchanges and hand-offs (the ticket agent's tool calling). V2 built the second
half deeply and the first half thinly:

- the after-sales "knowledge" is 6 structured rule documents (`policy_sources/`);
- `search_after_sales_policy` is token matching plus effective-date, category
  and priority selection over those 6 records;
- V1's retrieval (chunking, BM25 + embedding + RRF, rerank, evidence selection,
  refusal) is not used anywhere in V2.

M3 gives the product a real after-sales knowledge base that the agent searches
as a tool, and answers from it with citations, without weakening anything that
decides an action.

## Reference designs

| Project | What M3 takes from it |
|---|---|
| LangGraph customer-support tutorial (Swiss Airlines bot) | Policy retrieval is **a tool the model calls** (`lookup_policy`), next to booking tools; sensitive tools pause for a human. M3 follows the same shape: retrieval is a read tool; actions keep their existing gates. |
| OpenAI `openai-cs-agents-demo` | A dedicated FAQ role plus relevance / jailbreak guardrails. M3 keeps one decision policy; the existing `boundary` / `refuse` dispositions play the relevance role. |
| Sierra τ-bench / τ²-bench | Retail domain = returns / exchanges under a policy document, scored on final database state; **pass^k** (all k runs of a task correct) for reliability; τ²'s `banking_knowledge` domain is a knowledge-retrieval customer-service setting. M3 adds pass^k and uses `banking_knowledge` as a reference for corpus and task design. |

## Principle: retrieval explains, structured rules decide

| | Structured rules (unchanged) | Knowledge base (new) |
|---|---|---|
| Content | 6 versioned rules with machine-readable params | Platform rules, category notes, promotions, shipping and refund timing, evidence requirements, FAQ |
| Used by | Policy Guard, window arithmetic, grounding | The model (as a read tool) and the answer layer |
| Can it change an action outcome? | Yes | **Never** |

Two hard rules follow:

1. **KB text never grounds an action.** An order or item id that appears in a
   passage (examples, FAQ snippets) is not an observed business record; the
   grounding gate must keep requiring ids from `get_order` / `get_order_items`
   reads of the same run. Tested explicitly.
2. **KB numbers agree with the rules.** Where a document restates a
   decision-bearing number (a window length, a category exclusion), a
   build-time check requires it to equal the structured rule's params.

## Constraints found in the code

1. The model-visible tool list is fixed inside the frozen evaluated policy:
   `eval_v2/action_loop.py` offers `STAGE5_RUNTIME_TOOLS` only, and the tool
   descriptions live in frozen `aftersales/registry.py`. A new tool cannot be
   added without a policy of the product's own.
2. The frozen evidence derivation (`eval_v2/evidence.py`) accepts policy
   evidence only with a `policy_ref` validated against the structured catalog;
   passages cannot be returned through `search_after_sales_policy`.
3. Only `agent_core.py` may import `eval_v2`, only the reused modules; nothing
   in the product may subclass or patch `LLMNativeActionLoopPolicy`, `Guard`,
   `ActionGateway` or `ActionIntentValidator` (product boundary tests).
4. Embeddings need Ollama (`nomic-embed-text`, as in V1); DeepSeek has no
   embedding endpoint. Tests must not call Ollama.

## Design

### A product-owned decision policy, versioned

From M3 on, the product runs its own decision policy, `m3-decision/1`, in
`aftersales_service/decision_policy.py`: the evaluated Stage 6 loop's logic
(prompt, fail-closed parsing, one call per decision) carried over **by copy**,
with exactly one difference - one more read tool. The frozen Stage 6 policy
stays in `eval_v2` and remains selectable at the composition root
(`AFTERSALES_DECISION_POLICY=stage6|m3`, default `m3`), so:

- the Stage 6 results stay attributed to `v2-stage6-final` and reproducible;
- the M2 golden test keeps running against `stage6` unchanged;
- Stage 6 DEV can be run under both policies for a direct comparison.

### The new tool: `search_knowledge_base`

- Input: `query` (closed schema, like the other read tools). No identity, no
  order id.
- Execution: hybrid retrieval over the knowledge base (BM25 + embedding,
  RRF), filtered by business time (effective dates), category scope when the
  query names one, and precedence (an active promotion is never dropped in
  favour of the standard rule it overrides). Top passages returned as
  `Evidence` with `doc_id`, `version`, `locator`, effective dates.
- Degradation: Ollama unavailable -> BM25-only, flagged in the result; never a
  turn failure.
- The old `search_after_sales_policy` stays: it is the structured-rule lookup
  the Guard-consistent answers rely on.

Retrieval code is ported from V1 `rag.py` into `aftersales_service/knowledge_base.py`;
V1 stays read-only.

### Answer layer

The answer layer becomes product-owned too (`m3-answer/1`): sources are the
observed business records, structured rules **and** KB passages from this
run's observations; citations must reference offered sources; insufficient
evidence -> refusal. V1's refusal checks (unsupported identifiers, ungrounded
quantities) are ported where they apply.

## Corpus

- `knowledge_base/`, one Markdown file per document, front matter: `doc_id`,
  `title`, `doc_type` (rule / category / promotion / logistics / refund /
  evidence / faq), `scope`, `effective_from`, `effective_to`, `version`,
  optional `restates` (`policy_id`).
- 60-100 documents with realistic overlap, near-duplicates, promotions that
  override standard rules for a period, category exceptions. Design informed
  by τ²-bench `banking_knowledge`.
- Drafted with model help, **reviewed by the user**, then frozen before the
  eval sets are written.

## Evaluation

Datasets frozen before tuning; holdouts written in an isolated context and
opened once.

| Set | Size | Purpose |
|---|---|---|
| KB-DEV | ~45 | Policy questions: single / multi-doc, promotion precedence, effective-date edges, category exceptions, unanswerable, mixed with an order |
| KB-HOLDOUT | ~25 | Same mix, sealed |
| Stage 6 DEV (existing, 40) | 40 | Safety regression of `m3` against `stage6` |

Metrics:

- Retrieval: Recall@1/3/5 vs gold `doc_id`; ablation BM25 / vector / hybrid /
  hybrid + rerank (rerank kept only if DEV shows it pays for a model call).
- Tool use: on KB-DEV, share of policy questions where the model calls
  `search_knowledge_base` before answering.
- Answers (real DeepSeek): key-fact correctness, citation validity, refusal on
  unanswerable, **0 contradictions with the structured rule** (hard).
- Reliability: **pass^3** on KB-DEV and Stage 6 DEV.
- Safety: on Stage 6 DEV, the 6 hard invariants hold for `m3` in every run;
  end-to-end and final-state reported side by side with `stage6`. Any
  invariant violation blocks the milestone.
- Unchanged: 9 crash scenarios, all boundary tests,
  `git diff -- aftersales eval_v2 eval` empty.

## Phases

| Phase | Deliverable and acceptance | CC time |
|---|---|---:|
| 0. Spike | Policy copy with the extra tool behind the switch; 3 hand-written docs; a real DeepSeek run on ~5 policy questions: does the model call the tool, do citations validate, does `stage6` stay byte-identical (golden). Check how the frozen evidence derivation reacts to an unknown tool's observations. Measure Ollama latency on Windows. | 45-75 min |
| 1. Corpus | 60-100 docs; loader, heading-aware chunker, index with an embedding cache keyed by model + content hash (offline tests use the cache); restated-number consistency check; user review. | 1.5-3 h + review |
| 2. Eval sets | KB-DEV; KB-HOLDOUT authored in isolation and sealed. Before any retrieval tuning. | 1-1.5 h |
| 3. Retrieval | Hybrid retrieval with time / scope / precedence filters; ablation on DEV; rerank decision. | 1-2 h |
| 4. Policy + answer layer | `m3-decision/1` and `m3-answer/1` complete; "KB text never grounds an action" test; BM25 fallback; frontend shows KB citations. | 2-3 h |
| 5. Results | Stage 6 DEV regression (`m3` vs `stage6`, pass^3); KB-DEV run; KB-HOLDOUT opened once; results section; README; resume line. | 1.5-2 h |

Total about 8-12 h of CC time, 3 calendar days including your review.

## Out of scope

Multi-agent routing; using passages in Guard or grounding decisions; changing
the frozen Stage 6 policy, evidence derivation or datasets; reopening the Stage
6 holdout; a vector database (an in-process index is enough at this size).

## Risks

- **Corpus quality decides everything.** Phase 1 review is a real gate.
- **The copy drifts from the evaluated loop.** Mitigated by the switch: Stage 6
  DEV under both policies must show no safety regression; a diff of the two
  prompts and offered-tool lists is part of the Phase 4 report.
- **More tools, more wrong calls.** The model may search the KB when it should
  read an order, or vice versa. Measured by the tool-use metric and pass^3.
- **Ollama on the demo machine.** BM25 fallback; README and demo scripts start
  Ollama.
