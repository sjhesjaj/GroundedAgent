# GroundedAgent V2 · Stage 5 共享 generation / citation / E2E 设计

> 状态：共享 generation 核心（PR `stage5-shared-generation`）。只含实现与合成测试，尚未做任何 DEV / VALIDATION generation 评测。

## 1. 对照点与共享原则

- 冻结的控制策略：
  - Stage 4 Baseline：tag **`v2-stage4-baseline`** → `f99d5c3`；
  - Stage 5 Tool Loop：tag **`v2-stage5-tool-loop`** → `03b1893`。
- **只有一个** generation 实现（`eval_v2/generation.py`），以完全相同的方式用于两个冻结策略的控制记录。
  `generation.py` 与 `eval_v2/e2e.py` 不导入任何策略实现，也不按策略类型分支。
- 流水线：

  ```
  control policy -> CaseRunRecord -> EvidenceState -> 共享 generation -> citation 评分 -> E2E 评分
  ```

## 2. 最终 disposition 归控制策略所有

- generation 不选择 answer / refuse / handoff / boundary，只服从 `CaseRunRecord.final_disposition`。
- `answer`：调用一次 DeepSeek 生成回答与结构化 citation refs。
- `refuse` / `handoff` / `boundary`：确定性的固定文本，不调用模型，不带 citation，不声称发生过任何操作：
  - refuse：「根据现有证据无法可靠回答。」
  - handoff：「该问题需要人工进一步处理。」
  - boundary：「当前只读能力无法执行该操作。」
- 控制运行没有正常结束或没有 disposition（例如 unanswered_clarification、max_steps_exceeded）：返回结构化的
  `not_generated` 结果，不当作基础设施错误。

## 3. 输入边界

- generation 只接收：投递给策略的用户消息、`CaseRunRecord`、`EvidenceState`。从不接收 eval case。
- `CaseRunRecord` 只存用户文本的 SHA-256。E2E 编排从 `case["user_turns"]` 按 `UserMessageRecord.case_turn_index`
  取回原文，并要求 `SHA-256(UTF-8 原文) == text_sha256`，否则抛 `E2EIntegrityError`；generation 自己也会再核对一次。
- generation 从不调用业务工具、不打开数据库、不读取 expected 标签、archetype、fault 声明或任何数据集 / holdout 文件。

## 4. 事实来源：EvidenceState

- `EvidenceState.evidence_items` 是唯一的事实来源，按其现有的确定性顺序渲染为 source。
- 每个 source 只含紧凑的已发布事实：`ref`、`producer`、`source_type`、`locator`、`content`、`version`、`observed_at`。
  不含 metadata、trace、SQL、受信身份、case id、fault 声明；ToolResult 的错误信息不是证据（错误结果没有 evidence item）。
- source 内容是**不受信任的数据**：放在数据消息里，永不进入固定的 system prompt；system prompt 明确说明证据中可能
  出现类似指令的文字，绝不能执行。不做关键词过滤。

## 5. Answer 生成协议

- 只有 `final_disposition == "answer"` 调用模型。正式 generation 只用 DeepSeek（`SharedGenerator(provider, formal=True)`
  要求 `provider.name == "deepseek"`）。
- 固定参数：temperature **0**；thinking 由现有 provider 关闭；**`GENERATION_MAX_TOKENS = 512`**。
- 消息：固定 system prompt + 一条数据消息（canonical JSON：`business_time`、`user_messages`、`sources`）。
- `response_format` 为 JSON Schema：`{"answer": string, "citation_refs": string[] (uniqueItems)}`，`additionalProperties: false`。
- 校验（不修补模型输出，违反即 `GenerationProtocolError(code)`）：
  - `malformed_json`：不是合法 JSON；
  - `not_an_object`：不是 JSON 对象；
  - `invalid_keys`：键不恰好是 `answer` 与 `citation_refs`；
  - `empty_answer`：answer 不是非空字符串；
  - `citation_refs_not_a_list`；
  - `invalid_citation_ref`：ref 不是非空字符串；
  - `duplicate_citation_ref`；
  - `unknown_citation_ref`：ref 不在所提供的 source 中。
- provider / 网络错误原样向上抛出。不保存 reasoning。
- `GenerationResult`：`schema`、`status`（generated / fixed / not_generated）、`disposition`、`answer`、`citation_refs`、
  `provider`、`model`、`prompt_tokens`、`completion_tokens`、`latency_seconds`。

## 6. 标签分离

E2E 编排的固定顺序（有测试覆盖）：

```
record     = run_case(case, policy, max_steps)
state      = derive_evidence_state(record)
delivered  = delivered_user_messages(case, record)     # 只读 user_turns
generation = generator.generate(delivered, record, state)
--- 只有到这里之后才读取 expected_* 标签 ---
control    = score_case(case, record, state)
citation   = score_citations(case["expected_evidence"], state, generation)
```

## 7. Citation 评分（`citation_grounding_ok`）

- 对生成的 answer：取 `EvidenceState.evidence_items` 中 ref 出现在 `citation_refs` 里的子集，复用冻结的
  `score_evidence` 匹配器。
- grounding 成立当且仅当：全部 ref 有效；被引用的子集满足 `all_of` 与每个 `any_of` 组；被引用的子集不含 forbidden 证据。
- **forbidden 证据**：仅仅检索到（控制层的 `forbidden_evidence_present`）是诊断信息，不影响 grounding；
  **引用**了 forbidden 证据则 grounding 失败（`forbidden_citation_used = true`）。
- 非 answer 的固定回复不需要也不带 citation，grounding 视为成立；not_generated 与 generation 协议错误不成立。
- **局限**：`citation_grounding_ok` 衡量的是回答所依据的证据是否正确，**不等于语义上的回答正确性或事实准确性**——
  一句话仍可能误用了被正确引用的证据。

## 8. E2E 评分

- `control_success`：原样复制控制层评分，不重新定义。
- `generation_ok`：generated 或 fixed 为真；not_generated 或 `GenerationProtocolError` 为假（协议错误以稳定 code 记录在
  `generation_error`，不中断数据集）。
- `e2e_grounded_success = control_success AND generation_ok AND citation_grounding_ok`。
- 同时报告 `forbidden_citation_used`。

## 9. 数据使用与冻结

- generator 的开发只能使用 DEV 与规格说明；不得根据 validation 或 holdout 的单条 case 调优。
- holdout 打开之前，generator 与 evaluator 必须冻结。
- validation / holdout 不能用于调整 generation。
- 本 PR 只包含核心实现与合成 / mock 测试：generation DEV 运行 = 0，validation generation 运行 = 0，holdout 运行 = 0。
