# GroundedAgent V2 M3: after-sales knowledge base RAG

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

A customer asking "双十一买的羽绒服现在还能退吗，运费谁出" or "定制商品质量有问题怎么办"
gets an answer built from at most a few structured fields. M3 gives the product
a real after-sales knowledge base and answers from it with citations, without
weakening anything that decides an action.

## Principle: retrieval explains, structured rules decide

Two sources, two jobs, never mixed:

| | Structured rules (unchanged) | Knowledge base (new) |
|---|---|---|
| Content | 6 versioned rules with machine-readable params (windows, non-returnable categories, hand-off) | Platform rules, category notes, promotions, shipping and refund timing, evidence requirements, FAQ |
| Used by | Policy Guard, window arithmetic, the frozen evidence derivation | The answer layer only |
| Can it change an action outcome? | Yes | **Never** |

A passage can explain a rule; it can never be the reason an action is allowed.
Where the knowledge base restates a decision-bearing number (a window length, a
category exclusion), a build-time consistency check requires it to equal the
structured rule's params (see Phase 1).

## Constraints found in the code

1. **The policy tool cannot simply return passages.** The frozen evidence
   derivation (`eval_v2/evidence.py`) requires every `search_after_sales_policy`
   evidence item to carry a `policy_ref` and passes them to
   `validate_policy_refs`; a passage would raise `EvalEvidenceError`, and the
   answer turns into `answer_unavailable`.
2. **The model-visible tool list is frozen** (the evaluated
   `LLMNativeActionLoopPolicy` and its schemas). Adding a new read tool changes
   every model request.
3. **Only `agent_core.py` may import `eval_v2`, and only the reused modules**
   (`test_only_agent_core_imports_eval_v2_and_only_the_reused_modules`).
4. **Embeddings need Ollama** (`nomic-embed-text`, V1 `rag.embed_many`); DeepSeek
   has no embedding endpoint. Tests must not call Ollama (AGENTS.md).
5. The composition root already accepts another policy adapter
   (`build_runtime_registry(policy_adapter=...)`), but by (1) that seam is for
   structured rules only.

## Design: knowledge retrieval in the answer node

The control loop, tool list, Guard, grounding gate and gateway stay exactly as
they are. The change is in the `finish` node, when the disposition is `answer`:

```
decide -> ... -> finish(answer)
                   |
                   |-- observed evidence (orders, logistics, structured rules)   unchanged
                   |-- KB retrieval for the customer's question                 NEW
                   |     hybrid: BM25 + embedding, RRF; filtered by business time,
                   |     category in scope, precedence (promotion before standard)
                   |
                   +-- product answer generation over both, citations only to
                       offered sources, refusal when evidence is insufficient
```

- New product modules: `aftersales_service/knowledge_base.py` (corpus loading,
  chunking, index, retrieval) and the answer composition in `agent_core.py`
  (constraint 3). Retrieval code is ported from V1 `rag.py` into the product
  package, not imported from V1, so V1 stays read-only history.
- Sources offered to the model carry a kind: `observed` (as today) or `kb`.
  Citation validation stays strict: an answer may cite only offered refs.
- If the knowledge base is unavailable (no index, Ollama down), retrieval
  degrades to BM25-only and the trace says so; it never fails the turn.
- Refusal rules carry over from V1: no supporting passage, no answer.
- Router alternative (classify "question" vs "request" before the loop) was
  considered and rejected: it duplicates a decision the evaluated policy
  already makes, and mixed messages ("退货规则是什么，帮我退了") would split badly.

## Corpus

- Location: `knowledge_base/` (new, tracked). One Markdown file per document,
  front matter: `doc_id`, `title`, `doc_type` (rule / category / promotion /
  logistics / refund / evidence / faq), `scope` (categories, empty = all),
  `effective_from`, `effective_to`, `version`, optional `restates`
  (`policy_id` of a structured rule it explains).
- Size: 60-100 documents, written to be realistic and mutually consistent:
  overlapping wording, near-duplicates, promotions that override standard
  rules for a period, category exceptions. Size is the point: retrieval on 6
  documents would prove nothing.
- Authoring: drafted with model help, **reviewed by the user** before the eval
  sets are written. Every number that restates a structured rule is checked
  by code (Phase 1).

## Evaluation

Methodology as in Stage 6: datasets frozen before tuning, holdout written in an
isolated context and opened once.

| Set | Size | Content |
|---|---|---|
| KB-DEV | ~45 | Single-doc, multi-doc, promotion vs standard precedence, effective-date edge cases, category exceptions, unanswerable, mixed with an order context |
| KB-HOLDOUT | ~25 | Same mix, written without seeing DEV results or the implementation; sealed |

Metrics:

- Retrieval: Recall@1 / @3 / @5 against gold `doc_id`s; ablation BM25-only,
  vector-only, hybrid (RRF), hybrid + rerank. Rerank is kept only if DEV shows
  it is worth a model call per question.
- Answers (real DeepSeek): key-fact correctness, citation validity (every
  citation offered and supporting), refusal on unanswerable, and **rule
  consistency: 0 answers contradicting the structured rule for that category
  and business time** (hard invariant).
- Unchanged: the 9 crash scenarios, all safety and boundary tests, and
  `git diff -- aftersales eval_v2 eval` empty.

## Phases

| Phase | Deliverable and acceptance | CC time |
|---|---|---:|
| 0. Spike | Confirm the answer-node seam end to end with 3 hand-written docs: sources of both kinds reach the model, citations validate, the frozen tests pass. Measure Ollama embedding latency on the Windows machine. Decide the golden policy (below). | 30-45 min |
| 1. Corpus | 60-100 docs with front matter; loader, chunker (heading-aware), index build with an embedding cache keyed by model + content hash (offline tests read the cache); consistency check of restated numbers; user review. | 1.5-3 h + review |
| 2. Eval sets | KB-DEV authored; KB-HOLDOUT authored in an isolated context and sealed. **Before** any retrieval tuning. | 1-1.5 h |
| 3. Retrieval | Hybrid retrieval with time / scope / precedence filters; ablation report on DEV; rerank decision. | 1-2 h |
| 4. Answer layer | KB sources in the `finish(answer)` node, citation validation, refusal, BM25 fallback; frontend shows KB citations (existing citation panel). | 1.5-2 h |
| 5. Results | One DEV run, then the holdout opened once with real DeepSeek + Ollama; results section; README; resume line. | 1-1.5 h |

Total about 7-11 h of CC time, 2-3 calendar days including your review.

**Golden file.** M3 changes the generation request of answer turns by design.
The golden test is re-recorded once in Phase 4 and the diff is reviewed: only
answer-turn generation requests may change; decision requests, tool calls,
action outcomes and HTTP payloads of non-answer turns must stay byte-identical.

## Out of scope

Changing the tool list or the evaluated action loop; using passages in Guard
or grounding decisions; reopening the Stage 6 holdout; multi-turn query
rewriting beyond what V1 already had; a vector database (an in-process index
is enough at this size).

## Risks

- **Corpus quality decides everything.** A thin or self-contradictory corpus
  makes the metrics meaningless. Phase 1 review is a real gate.
- **Model may skip `finish(answer)`.** If the evaluated policy answers policy
  questions by handing off or refusing, KB retrieval never runs. Phase 0 checks
  how often this happens on a few real questions; if it is frequent, the
  answer-node seam is the wrong place and the plan is revisited before Phase 1.
- **Ollama on the demo machine.** Covered by the BM25 fallback, but the demo
  script must start Ollama and the README must say so.
